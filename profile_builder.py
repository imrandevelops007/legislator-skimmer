import os
import re
import json
import time
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Tuple

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from google import genai


# =========================
# Config
# =========================

SHEET_ID = os.environ["SHEET_ID"]
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
ONLY_LEGISLATOR = os.getenv("ONLY_LEGISLATOR", "").strip()

MAX_BILLS_PER_LEGISLATOR = int(os.getenv("MAX_BILLS_PER_LEGISLATOR", "8"))
MIN_SUBSTANTIVE_BILLS_REQUIRED = int(os.getenv("MIN_SUBSTANTIVE_BILLS_REQUIRED", "4"))

PROFILE_MAX_RETRIES = int(os.getenv("PROFILE_MAX_RETRIES", "2"))
PROFILE_REQUEST_DELAY_SECONDS = float(os.getenv("PROFILE_REQUEST_DELAY_SECONDS", "5"))

STOP_ON_QUOTA_EXHAUSTION = os.getenv("STOP_ON_QUOTA_EXHAUSTION", "true").strip().lower() == "true"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

TAB_ACTIVITY = os.getenv("TAB_ACTIVITY", "Activity_Items")
TAB_METADATA = os.getenv("TAB_METADATA", "Legislator_Metadata")
TAB_PROFILES = os.getenv("TAB_PROFILES", "Profiles_Dynamic")


# =========================
# Profiles_Dynamic columns
# =========================

PROFILE_COLUMNS = [
    "Legislator",
    "Committee_Relevance_Summary",
    "Time_In_Office_Summary",
    "Generated_Biography",
    "Key_Issues",
    "District_Development_Signals",
    "Legislative_Focus_Areas",
    "Key_Bills",
    "Political_Positioning",
    "Political_Positioning_Bullets",
    "SBDC_Framing",
    "Talking_Points",
    "Bills_Analyzed_Count",
    "Source_Bill_Numbers",
    "Last_Updated",
    "Profile_Processed",
    "Notes",
    "Needs_Rebuild",
]


# =========================
# Helpers
# =========================

def normalize_header(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def bool_from_cell(value: Any) -> bool:
    return clean(value).lower() in {"true", "yes", "1", "y"}


def now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def bullets_to_multiline(items: List[str]) -> str:
    cleaned = [clean(item) for item in items if clean(item)]
    return "\n".join(f"• {item}" for item in cleaned)


def looks_like_quota_error(text: str) -> bool:
    t = clean(text).lower()
    return (
        "resource_exhausted" in t
        or "quota exceeded" in t
        or "429" in t
        or "generate_content_free_tier_requests" in t
    )


def looks_like_temporary_unavailable(text: str) -> bool:
    t = clean(text).lower()
    return "503" in t or "unavailable" in t or "high demand" in t


def looks_like_error_output(text: str) -> bool:
    t = clean(text).lower()
    return (
        t.startswith("error:")
        or "resource_exhausted" in t
        or "quota exceeded" in t
        or "429" in t
        or "traceback" in t
        or "exception" in t
        or "503" in t
    )


def parse_json_response(text: str) -> Dict[str, Any]:
    text = clean(text)

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text.strip(), flags=re.IGNORECASE).strip()
        text = re.sub(r"```$", "", text.strip()).strip()

    return json.loads(text)


# =========================
# Google Clients
# =========================

def get_service_account_credentials() -> Credentials:
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    return Credentials.from_service_account_info(info, scopes=SCOPES)


def get_sheets_service():
    creds = get_service_account_credentials()
    return build("sheets", "v4", credentials=creds)


def get_gemini_client():
    return genai.Client(api_key=GEMINI_API_KEY)


# =========================
# Sheets I/O
# =========================

def get_sheet_values(service, tab_name: str) -> List[List[str]]:
    resp = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=tab_name,
    ).execute()
    return resp.get("values", [])


def rows_as_dicts_with_headers(service, tab_name: str) -> Tuple[List[str], List[Dict[str, Any]]]:
    values = get_sheet_values(service, tab_name)
    if not values:
        return [], []

    raw_headers = values[0]
    norm_headers = [normalize_header(h) for h in raw_headers]
    rows: List[Dict[str, Any]] = []

    for row_num, row in enumerate(values[1:], start=2):
        padded = row + [""] * (len(raw_headers) - len(row))
        item = {norm_headers[i]: padded[i] for i in range(len(raw_headers))}
        item["_row_number"] = row_num
        rows.append(item)

    return raw_headers, rows


def column_letter_from_index(index_1_based: int) -> str:
    result = ""
    while index_1_based > 0:
        index_1_based, rem = divmod(index_1_based - 1, 26)
        result = chr(65 + rem) + result
    return result


def make_header_index(raw_headers: List[str]) -> Dict[str, int]:
    return {normalize_header(h): i + 1 for i, h in enumerate(raw_headers)}


def ensure_profiles_headers(service) -> Tuple[List[str], Dict[str, int]]:
    raw_headers, _ = rows_as_dicts_with_headers(service, TAB_PROFILES)

    if not raw_headers:
        service.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"{TAB_PROFILES}!A1",
            valueInputOption="RAW",
            body={"values": [PROFILE_COLUMNS]},
        ).execute()
        raw_headers = PROFILE_COLUMNS

    existing_norm = [normalize_header(h) for h in raw_headers]
    missing = []

    for required in PROFILE_COLUMNS:
        if normalize_header(required) not in existing_norm:
            missing.append(required)

    if missing:
        updated_headers = raw_headers + missing
        service.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"{TAB_PROFILES}!A1",
            valueInputOption="RAW",
            body={"values": [updated_headers]},
        ).execute()
        raw_headers = updated_headers

    return raw_headers, make_header_index(raw_headers)


def find_profile_row(rows: List[Dict[str, Any]], legislator: str) -> Optional[Dict[str, Any]]:
    target = clean(legislator).lower()
    for row in rows:
        if clean(row.get("legislator")).lower() == target:
            return row
    return None


def append_new_profile_row(service, legislator: str) -> int:
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=f"{TAB_PROFILES}!A:A",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [[legislator]]},
    ).execute()

    _, rows = rows_as_dicts_with_headers(service, TAB_PROFILES)
    row = find_profile_row(rows, legislator)
    if not row:
        raise RuntimeError(f"Failed to append new profile row for {legislator}")
    return int(row["_row_number"])


def batch_update_profile_row(
    service,
    row_number: int,
    header_index: Dict[str, int],
    values_by_header: Dict[str, str],
) -> None:
    data = []

    for header_name, value in values_by_header.items():
        norm = normalize_header(header_name)
        if norm not in header_index:
            continue

        col_letter = column_letter_from_index(header_index[norm])
        data.append({
            "range": f"{TAB_PROFILES}!{col_letter}{row_number}",
            "values": [[value]],
        })

    if not data:
        return

    service.spreadsheets().values().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={
            "valueInputOption": "RAW",
            "data": data,
        },
    ).execute()


# =========================
# Data Selection
# =========================

def is_substantive_bill_number(bill_number: str) -> bool:
    bill_number = clean(bill_number).upper()
    return bill_number.startswith("HB") or bill_number.startswith("SB")


def score_bill(row: Dict[str, Any]) -> int:
    bill_number = clean(row.get("bill_number"))
    title = clean(row.get("bill_title")).lower()
    summary = clean(row.get("bill_summary")).lower()

    score = 0

    if is_substantive_bill_number(bill_number):
        score += 100
    elif bill_number.startswith("HR") or bill_number.startswith("SR"):
        score -= 50

    noisy_terms = [
        "tribute",
        "memorial",
        "commemorate",
        "recognize",
        "resolution of tribute",
        "congratulating",
        "honoring",
    ]
    if any(term in title or term in summary for term in noisy_terms):
        score -= 40

    if summary:
        score += 20
    if title:
        score += 10

    return score


def get_legislator_activity(rows: List[Dict[str, Any]], legislator: str) -> List[Dict[str, Any]]:
    target = clean(legislator).lower()
    filtered = [r for r in rows if clean(r.get("legislator")).lower() == target]

    enriched = [
        r for r in filtered
        if clean(r.get("bill_title")) and clean(r.get("bill_summary"))
    ]

    enriched.sort(key=score_bill, reverse=True)
    return enriched


def select_best_bills(rows: List[Dict[str, Any]], max_count: int) -> List[Dict[str, Any]]:
    substantive = [r for r in rows if is_substantive_bill_number(r.get("bill_number", ""))]
    non_substantive = [r for r in rows if not is_substantive_bill_number(r.get("bill_number", ""))]

    selected = substantive[:max_count]
    if len(selected) < max_count:
        selected.extend(non_substantive[: max_count - len(selected)])

    return selected[:max_count]


# =========================
# Prompting
# =========================

def build_legislator_prompt(metadata: Dict[str, Any], bills: List[Dict[str, Any]]) -> str:
    legislator = clean(metadata.get("legislator"))
    chamber = clean(metadata.get("chamber"))
    district = clean(metadata.get("district"))
    party = clean(metadata.get("party"))
    term_dates = clean(metadata.get("term_dates"))
    time_in_office_notes = clean(metadata.get("time_in_office_notes"))
    education = clean(metadata.get("education"))
    professional_background = clean(metadata.get("professional_background"))
    government_experience = clean(metadata.get("government_experience"))
    committee_assignments = clean(metadata.get("committee_assignments"))
    counties = clean(metadata.get("counties"))
    sources = clean(metadata.get("sources_and_verification_notes") or metadata.get("sources"))

    bill_lines = []
    for idx, bill in enumerate(bills, start=1):
        bill_lines.append(
            f"""Bill {idx}
Bill Number: {clean(bill.get("bill_number"))}
Bill Title: {clean(bill.get("bill_title"))}
Bill Summary: {clean(bill.get("bill_summary"))}
URL: {clean(bill.get("url"))}
"""
        )

    bills_block = "\n".join(bill_lines)

    return f"""
You are generating a concise, executive-facing legislator briefing profile for Michigan SBDC outreach planning.

Your job is to create a structured intelligence profile that is:
- highly skimmable
- bullet-based
- grounded in the provided metadata and bill data
- strategically useful for outreach
- not redundant across sections
- cautious about political inference
- free of filler and generic language

Do not invent facts.
Do not include uncertainty language unless necessary.
Do not write long paragraphs.
Do not repeat the same bill or idea across multiple sections unless truly necessary.

Return valid JSON only.
No markdown.
No code fences.

Use exactly these keys:
committee_relevance_summary
time_in_office_summary
generated_biography
key_issues
district_development_signals
legislative_focus_areas
key_bills
political_positioning
political_positioning_bullets
sbdc_framing
talking_points

Each value must be a list of concise bullet strings.

Metadata:
Legislator: {legislator}
Chamber: {chamber}
District: {district}
Party: {party}
Term dates: {term_dates}
Time in office notes: {time_in_office_notes}
Education: {education}
Professional background: {professional_background}
Government experience: {government_experience}
Committee assignments: {committee_assignments}
Counties: {counties}
Sources / verification notes: {sources}

Bills:
{bills_block}

Rules:
1. committee_relevance_summary:
   Explain why the legislator's committees matter for SBDC or economic development conversations.

2. time_in_office_summary:
   Summarize service timeline and relevant term context.

3. generated_biography:
   Use grounded identity/context bullets such as education, prior work, and public service background.

4. key_issues:
   Identify issue areas consistently reflected in the bill set.

5. district_development_signals:
   Infer district-relevant development patterns carefully from counties, region, committee context, and bill themes.

6. legislative_focus_areas:
   Focus on what they appear to be actively working on legislatively.

7. key_bills:
   Highlight the strongest or most relevant substantive bills with brief plain-English explanations.

8. political_positioning:
   Give 1 to 2 concise framing bullets describing practical ideological or policy tendencies, only if evidence supports it.

9. political_positioning_bullets:
   Provide additional supporting bullets for positioning, still cautious and evidence-based.

10. sbdc_framing:
    Explain how SBDC could frame value or impact to this legislator.

11. talking_points:
    Provide strategic outreach angles, not scripts.

Output must be valid JSON in this shape:
{{
  "committee_relevance_summary": ["...", "..."],
  "time_in_office_summary": ["...", "..."],
  "generated_biography": ["...", "..."],
  "key_issues": ["...", "..."],
  "district_development_signals": ["...", "..."],
  "legislative_focus_areas": ["...", "..."],
  "key_bills": ["...", "..."],
  "political_positioning": ["...", "..."],
  "political_positioning_bullets": ["...", "..."],
  "sbdc_framing": ["...", "..."],
  "talking_points": ["...", "..."]
}}
""".strip()


# =========================
# Gemini
# =========================

def call_gemini_with_retries(client, prompt: str) -> str:
    last_error = None

    for attempt in range(1, PROFILE_MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
            )
            text = clean(getattr(response, "text", ""))
            if not text:
                raise RuntimeError("Gemini returned empty text.")
            return text

        except Exception as e:
            error_text = str(e)
            last_error = e

            if looks_like_quota_error(error_text):
                print(f"Quota exhausted while building profile: {error_text}")
                if STOP_ON_QUOTA_EXHAUSTION:
                    raise
                return ""

            if looks_like_temporary_unavailable(error_text):
                print(f"Gemini temporarily unavailable (attempt {attempt}/{PROFILE_MAX_RETRIES}): {error_text}")
            else:
                print(f"Profile generation attempt {attempt} failed: {error_text}")

            if attempt < PROFILE_MAX_RETRIES:
                time.sleep(PROFILE_REQUEST_DELAY_SECONDS)

    raise RuntimeError(f"Gemini profile generation failed after retries: {last_error}")


# =========================
# Validation
# =========================

def normalize_profile_json(data: Dict[str, Any]) -> Dict[str, List[str]]:
    required_keys = {
        "committee_relevance_summary",
        "time_in_office_summary",
        "generated_biography",
        "key_issues",
        "district_development_signals",
        "legislative_focus_areas",
        "key_bills",
        "political_positioning",
        "political_positioning_bullets",
        "sbdc_framing",
        "talking_points",
    }

    normalized: Dict[str, List[str]] = {}

    for key in required_keys:
        value = data.get(key, [])
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            value = []

        cleaned_items = [clean(v) for v in value if clean(v)]
        normalized[key] = cleaned_items

    return normalized


def profile_is_valid(profile: Dict[str, List[str]]) -> bool:
    if not profile:
        return False

    required_nonempty = [
        "committee_relevance_summary",
        "generated_biography",
        "key_issues",
        "legislative_focus_areas",
        "key_bills",
        "sbdc_framing",
        "talking_points",
    ]

    for key in required_nonempty:
        items = profile.get(key, [])
        if not items:
            return False

        joined = " ".join(items)
        if looks_like_error_output(joined):
            return False

    return True


# =========================
# Main Logic
# =========================

def build_metadata_index(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = clean(row.get("legislator")).lower()
        if key:
            index[key] = row
    return index


def build_legislator_list(metadata_rows: List[Dict[str, Any]], activity_rows: List[Dict[str, Any]]) -> List[str]:
    names = set()

    for row in metadata_rows:
        name = clean(row.get("legislator"))
        if name:
            names.add(name)

    for row in activity_rows:
        name = clean(row.get("legislator"))
        if name:
            names.add(name)

    ordered = sorted(names)

    if ONLY_LEGISLATOR:
        ordered = [name for name in ordered if name.lower() == ONLY_LEGISLATOR.lower()]

    return ordered


def should_rebuild_profile(existing_profile_row: Optional[Dict[str, Any]]) -> bool:
    if not existing_profile_row:
        return True

    processed = bool_from_cell(existing_profile_row.get("profile_processed"))
    needs_rebuild = bool_from_cell(existing_profile_row.get("needs_rebuild"))

    if not processed:
        return True
    if needs_rebuild:
        return True

    return False


def main():
    print("Connecting to Google Sheets and Gemini...")
    sheets_service = get_sheets_service()
    gemini_client = get_gemini_client()

    print("Loading tabs...")
    _, metadata_rows = rows_as_dicts_with_headers(sheets_service, TAB_METADATA)
    _, activity_rows = rows_as_dicts_with_headers(sheets_service, TAB_ACTIVITY)

    if not metadata_rows:
        raise RuntimeError(f"No rows found in {TAB_METADATA}")
    if not activity_rows:
        raise RuntimeError(f"No rows found in {TAB_ACTIVITY}")

    profile_headers, profile_header_index = ensure_profiles_headers(sheets_service)
    _, profile_rows = rows_as_dicts_with_headers(sheets_service, TAB_PROFILES)

    metadata_index = build_metadata_index(metadata_rows)
    legislators = build_legislator_list(metadata_rows, activity_rows)

    print(f"Legislators to evaluate: {len(legislators)}")

    for legislator in legislators:
        print(f"\nEvaluating {legislator}")

        existing_profile_row = find_profile_row(profile_rows, legislator)

        if not should_rebuild_profile(existing_profile_row):
            print(f"Skipping {legislator}: profile is already processed and does not need rebuild.")
            continue

        metadata = metadata_index.get(legislator.lower())
        if not metadata:
            print(f"Skipping {legislator}: no metadata row.")
            continue

        legislator_activity = get_legislator_activity(activity_rows, legislator)
        if not legislator_activity:
            print(f"Skipping {legislator}: no enriched activity.")
            continue

        selected_bills = select_best_bills(legislator_activity, MAX_BILLS_PER_LEGISLATOR)
        substantive_count = sum(
            1 for bill in selected_bills if is_substantive_bill_number(bill.get("bill_number", ""))
        )

        if substantive_count < MIN_SUBSTANTIVE_BILLS_REQUIRED:
            print(
                f"Skipping {legislator}: only {substantive_count} substantive bills available, "
                f"minimum required is {MIN_SUBSTANTIVE_BILLS_REQUIRED}."
            )
            continue

        prompt = build_legislator_prompt(metadata, selected_bills)

        try:
            raw_text = call_gemini_with_retries(gemini_client, prompt)
        except Exception as e:
            error_text = str(e)
            print(f"Profile generation failed for {legislator}: {error_text}")

            if looks_like_quota_error(error_text) and STOP_ON_QUOTA_EXHAUSTION:
                print("Stopping profile builder because quota was exhausted. Existing sheet data was preserved.")
                break

            print("Existing profile left unchanged.")
            continue

        if not raw_text:
            print(f"No profile text returned for {legislator}. Existing profile left unchanged.")
            continue

        if looks_like_error_output(raw_text):
            print(f"Gemini returned error-like output for {legislator}. Existing profile left unchanged.")
            continue

        try:
            parsed = parse_json_response(raw_text)
            normalized_profile = normalize_profile_json(parsed)
        except Exception as e:
            print(f"Failed to parse profile JSON for {legislator}: {e}")
            print("Existing profile left unchanged.")
            continue

        if not profile_is_valid(normalized_profile):
            print(f"Generated profile for {legislator} did not pass validation.")
            print("Existing profile left unchanged.")
            continue

        source_bill_numbers = []
        for bill in selected_bills:
            bill_number = clean(bill.get("bill_number"))
            if bill_number:
                source_bill_numbers.append(bill_number)

        row_values = {
            "Legislator": legislator,
            "Committee_Relevance_Summary": bullets_to_multiline(normalized_profile["committee_relevance_summary"]),
            "Time_In_Office_Summary": bullets_to_multiline(normalized_profile["time_in_office_summary"]),
            "Generated_Biography": bullets_to_multiline(normalized_profile["generated_biography"]),
            "Key_Issues": bullets_to_multiline(normalized_profile["key_issues"]),
            "District_Development_Signals": bullets_to_multiline(normalized_profile["district_development_signals"]),
            "Legislative_Focus_Areas": bullets_to_multiline(normalized_profile["legislative_focus_areas"]),
            "Key_Bills": bullets_to_multiline(normalized_profile["key_bills"]),
            "Political_Positioning": bullets_to_multiline(normalized_profile["political_positioning"]),
            "Political_Positioning_Bullets": bullets_to_multiline(normalized_profile["political_positioning_bullets"]),
            "SBDC_Framing": bullets_to_multiline(normalized_profile["sbdc_framing"]),
            "Talking_Points": bullets_to_multiline(normalized_profile["talking_points"]),
            "Bills_Analyzed_Count": str(len(selected_bills)),
            "Source_Bill_Numbers": ", ".join(source_bill_numbers),
            "Last_Updated": now_iso_utc(),
            "Profile_Processed": "TRUE",
            "Needs_Rebuild": "FALSE",
        }

        existing_notes = clean(existing_profile_row.get("notes")) if existing_profile_row else ""
        if existing_notes:
            row_values["Notes"] = existing_notes

        if existing_profile_row:
            target_row_number = int(existing_profile_row["_row_number"])
        else:
            target_row_number = append_new_profile_row(sheets_service, legislator)
            _, profile_rows = rows_as_dicts_with_headers(sheets_service, TAB_PROFILES)

        try:
            batch_update_profile_row(
                sheets_service,
                row_number=target_row_number,
                header_index=profile_header_index,
                values_by_header=row_values,
            )
            print(f"Profile updated safely for {legislator}.")
        except Exception as e:
            print(f"Write failed for {legislator}: {e}")
            print("Any existing prior data in the sheet was preserved until this final write step.")

        _, profile_rows = rows_as_dicts_with_headers(sheets_service, TAB_PROFILES)

        time.sleep(PROFILE_REQUEST_DELAY_SECONDS)

    print("\nProfile builder complete.")


if __name__ == "__main__":
    main()
