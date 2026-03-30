import os
import json
import time
import random
import re
from datetime import datetime, timezone
from typing import Dict, List, Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from google import genai


# =========================
# Config
# =========================
SHEET_ID = os.environ["SHEET_ID"]
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

MAX_BILLS_PER_LEGISLATOR = int(os.getenv("MAX_BILLS_PER_LEGISLATOR", "12"))
PROFILE_MAX_RETRIES = int(os.getenv("PROFILE_MAX_RETRIES", "5"))
PROFILE_REQUEST_DELAY_SECONDS = float(os.getenv("PROFILE_REQUEST_DELAY_SECONDS", "3"))
ONLY_LEGISLATOR = os.getenv("ONLY_LEGISLATOR", "").strip()
MIN_BILLS_REQUIRED = int(os.getenv("MIN_BILLS_REQUIRED", "3"))

METADATA_RANGE = "Legislator_Metadata!A2:P"
ACTIVITY_RANGE = "Activity_Items!A2:I"
PROFILES_RANGE = "Profiles_Dynamic!A2:Q"

# Legislator_Metadata columns:
# A  Legislator
# B  Chamber
# C  District
# D  Party
# E  First_Elected_to_Current_Chamber
# F  Current_Term_Start
# G  Current_Term_End
# H  Time_In_Office_Note
# I  Education
# J  Professional_Background
# K  Government_Experience
# L  Committee_Assignments
# M  Key_Issues_Source
# N  Political_Positioning_Source
# O  Verification_Notes
# P  Image_URL

# Profiles_Dynamic columns:
# A  Legislator
# B  Committee_Relevance_Summary
# C  Time_In_Office_Summary
# D  Generated_Biography
# E  Key_Issues
# F  District_Development_Signals
# G  Legislative_Focus_Areas
# H  Key_Bills
# I  Political_Positioning
# J  Political_Positioning_Bullets
# K  SBDC_Framing
# L  Talking_Points
# M  Bills_Analyzed_Count
# N  Source_Bill_Numbers
# O  Last_Updated
# P  Profile_Processed
# Q  Notes


# =========================
# Sheets helpers
# =========================
def get_sheets_service():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)


def sheets_get_values(service, rng: str) -> List[List[str]]:
    return (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SHEET_ID, range=rng)
        .execute()
        .get("values", [])
    )


def sheets_update_range(service, rng: str, values: List[List[str]]) -> None:
    service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=rng,
        valueInputOption="RAW",
        body={"values": values},
    ).execute()


def sheets_append_rows(service, rng: str, values: List[List[str]]) -> None:
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=rng,
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()


def pad_row(row: List[str], target_len: int) -> List[str]:
    return row + [""] * (target_len - len(row))


# =========================
# Load data
# =========================
def load_metadata(service) -> Dict[str, Dict[str, str]]:
    rows = sheets_get_values(service, METADATA_RANGE)
    out: Dict[str, Dict[str, str]] = {}

    for row in rows:
        row = pad_row(row, 16)
        legislator = row[0].strip()
        if not legislator:
            continue

        out[legislator] = {
            "legislator": legislator,
            "chamber": row[1].strip(),
            "district": row[2].strip(),
            "party": row[3].strip(),
            "first_elected_to_current_chamber": row[4].strip(),
            "current_term_start": row[5].strip(),
            "current_term_end": row[6].strip(),
            "time_in_office_note": row[7].strip(),
            "education": row[8].strip(),
            "professional_background": row[9].strip(),
            "government_experience": row[10].strip(),
            "committee_assignments": row[11].strip(),
            "key_issues_source": row[12].strip(),
            "political_positioning_source": row[13].strip(),
            "verification_notes": row[14].strip(),
            "image_url": row[15].strip(),
        }

    return out


def load_activity(service) -> Dict[str, List[Dict[str, str]]]:
    rows = sheets_get_values(service, ACTIVITY_RANGE)
    out: Dict[str, List[Dict[str, str]]] = {}

    for row in rows:
        row = pad_row(row, 9)
        url, legislator, item_type, timestamp, bill_number, bill_title, bill_summary, processed, notes = row

        legislator = legislator.strip()
        if not legislator:
            continue
        if item_type.strip().lower() != "bill":
            continue
        if processed.strip().upper() != "TRUE":
            continue
        if not bill_number.strip() or not bill_summary.strip():
            continue

        out.setdefault(legislator, []).append(
            {
                "url": url.strip(),
                "timestamp": timestamp.strip(),
                "bill_number": bill_number.strip(),
                "bill_title": bill_title.strip(),
                "bill_summary": bill_summary.strip(),
                "notes": notes.strip(),
            }
        )

    for legislator in out:
        out[legislator].sort(key=lambda x: x["timestamp"], reverse=True)

    return out


def load_existing_profiles(service) -> Dict[str, int]:
    rows = sheets_get_values(service, PROFILES_RANGE)
    out: Dict[str, int] = {}

    for idx, row in enumerate(rows, start=2):
        row = pad_row(row, 17)
        legislator = row[0].strip()
        if legislator:
            out[legislator] = idx

    return out


# =========================
# Bill prioritization
# =========================
def classify_bill_priority(bill_number: str) -> int:
    bill_number = (bill_number or "").upper().strip()

    if re.match(r"^(HB|SB)\s+\d+$", bill_number):
        return 1

    if re.match(r"^(HCR|SCR)\s+\d+$", bill_number):
        return 2

    if re.match(r"^(HR|SR)\s+\d+$", bill_number):
        return 3

    return 4


def select_best_bills(bills: List[Dict[str, str]], max_bills: int) -> List[Dict[str, str]]:
    buckets: Dict[int, List[Dict[str, str]]] = {1: [], 2: [], 3: [], 4: []}

    for bill in bills:
        priority = classify_bill_priority(bill["bill_number"])
        buckets[priority].append(bill)

    selected: List[Dict[str, str]] = []

    for priority in [1, 2, 3, 4]:
        for bill in buckets[priority]:
            if len(selected) >= max_bills:
                break
            selected.append(bill)
        if len(selected) >= max_bills:
            break

    return selected


# =========================
# Gemini helpers
# =========================
def build_client() -> genai.Client:
    if not GEMINI_API_KEY.strip():
        raise RuntimeError("GEMINI_API_KEY is missing.")
    return genai.Client(api_key=GEMINI_API_KEY)


def backoff_sleep(attempt: int):
    base = min(60, 5 * (2 ** (attempt - 1)))
    jitter = random.uniform(0, 1.5)
    wait_s = base + jitter
    print(f"Retrying after {wait_s:.1f}s...")
    time.sleep(wait_s)


def build_prompt(metadata: Dict[str, str], bills: List[Dict[str, str]]) -> str:
    instructions = """
You are a nonpartisan policy analyst creating a concise, highly skimmable legislator briefing for the Michigan SBDC.

Return ONLY valid JSON with exactly these keys:
- committee_relevance_summary
- time_in_office_summary
- generated_biography
- key_issues
- district_development_signals
- legislative_focus_areas
- key_bills
- political_positioning
- political_positioning_bullets
- sbdc_framing
- talking_points

Core objective:
Produce a sharp, executive-level briefing that can be skimmed in under 30 seconds. Prioritize clarity, brevity, and usefulness for outreach.

GENERAL STYLE RULES:
- Be concise and direct.
- Avoid filler, repetition, and unnecessary explanation.
- Prefer short sentences and tight phrasing.
- Each bullet must communicate one clear idea.
- Do NOT use paragraphs where bullets are appropriate.
- Avoid vague or generic language.
- Do not use markdown.
- Output JSON only.

TONE:
- Professional, confident, and analytical.
- Use direct language when evidence is clear.
- Avoid weak phrasing like "suggests", "demonstrates", or "appears" unless uncertainty is necessary.
- Avoid over-explaining.

GENERAL RULES:
- Never ignore available metadata fields.
- If committee_assignments is provided, it must be used.
- Use only the metadata and bill list provided.
- Do not invent facts.

INTERPRETATION RULES:
- Prioritize substantive policy legislation over ceremonial or commemorative resolutions.
- Do NOT let recognition resolutions dominate the profile unless they are the only available activity.
- Use metadata (committee assignments, background, experience) as primary signals of long-term priorities.
- Use legislation to reinforce or refine those signals.
- Treat ceremonial resolutions as weak signals.

ANTI-REDUNDANCY RULES:
- Do NOT repeat the same information across multiple sections.
- Each section must add new value.
- Do NOT restate biography in other sections.
- Avoid repeating the same issue or theme in slightly different wording.

FIELD REQUIREMENTS:

- committee_relevance_summary:
  - REQUIRED: Use the committee_assignments field from metadata if it is present
  - DO NOT say committee information is missing unless the field is completely empty
  - 1 to 2 short sentences OR 2 tight bullets (max)
  - Explain why their committees matter for business, economic development, regulation, or funding
  - Focus on practical policy relevance, not just list repetition
  - This section must be populated if committee data exists

- time_in_office_summary:
  - 2 to 3 short bullets
  - Focus on timeline and progression only
  - No extra commentary

- generated_biography:
  - 2 to 3 bullets
  - Education, professional background, and public service
  - No repetition of the same role

- key_issues:
  - 3 to 5 bullets
  - Format: "Issue: short explanation"
  - Focus on durable policy interests
  - No ceremonial or awareness-based issues unless unavoidable

- district_development_signals:
  - 2 to 4 bullets
  - Must be grounded in clear legislative or infrastructure implications
  - Avoid speculative phrases like "could signal"
  - Keep practical and concrete

- legislative_focus_areas:
  - 3 to 5 bullets
  - Format: "Focus Area: short explanation"
  - Reflect actual legislative behavior patterns
  - Prioritize policy over symbolic activity

- key_bills:
  - REQUIRED
  - array of 3 to 5 items
  - each item must include:
    - bill_number
    - summary
  - choose the most representative and substantive bills
  - one-sentence summaries only
  - do not prioritize ceremonial resolutions if substantive bills are available
  - this field must never be empty if bills are provided

- political_positioning:
  - Short label only
  - Examples:
    - "Center-right | Pro-business | Fiscal conservative"
    - "Center-left | Workforce-focused | Institutional"
  - Do NOT just restate party

- political_positioning_bullets:
  - 2 to 3 bullets MAX
  - Explain governing style and priorities
  - Be direct and specific

- sbdc_framing:
  - 2 to 3 short sentences
  - Must be actionable and strategic
  - Focus on alignment with:
    - small business growth
    - workforce
    - economic development
    - ROI / outcomes
  - No fluff

- talking_points:
  - 4 to 5 bullets
  - Each bullet must be:
    - short
    - actionable
    - specific to the legislator
  - Avoid generic statements

QUALITY BAR:
- The output should feel like a briefing memo, not an essay.
- A reader should understand the legislator in seconds.
- Prioritize clarity over completeness.
- Strong signal > more words.
""".strip()

    payload = {
        "metadata": metadata,
        "selected_recent_bills": bills,
    }

    return f"{instructions}\n\nINPUT:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"


def extract_json(text: str) -> Dict[str, Any]:
    text = (text or "").strip()

    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("Model did not return valid JSON.")

    return json.loads(text[start:end + 1])


def call_gemini(client: genai.Client, prompt: str) -> Dict[str, Any]:
    last_error = None

    for attempt in range(1, PROFILE_MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
            )
            return extract_json(getattr(response, "text", ""))
        except Exception as exc:
            last_error = exc
            msg = str(exc)

            if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "503" in msg or "UNAVAILABLE" in msg:
                if attempt < PROFILE_MAX_RETRIES:
                    backoff_sleep(attempt)
                    continue

            raise

    raise RuntimeError(f"Gemini failed after {PROFILE_MAX_RETRIES} retries: {last_error}")


# =========================
# Transform helpers
# =========================
def join_pipe(items: Any) -> str:
    if not isinstance(items, list):
        return ""
    return " | ".join([str(x).strip() for x in items if str(x).strip()])


def normalize_key_bills(items: Any, fallback_bills: List[Dict[str, str]]) -> str:
    """
    Convert Gemini key_bills output into:
    BILL_NUMBER::summary || BILL_NUMBER::summary

    If Gemini returns nothing usable, fall back to selected bills.
    """
    out: List[str] = []

    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue

            bill_number = str(
                item.get("bill_number")
                or item.get("bill")
                or item.get("number")
                or ""
            ).strip()

            summary = str(
                item.get("summary")
                or item.get("bill_summary")
                or item.get("description")
                or ""
            ).strip()

            if bill_number and summary:
                out.append(f"{bill_number}::{summary}")

    if out:
        return " || ".join(out)

    fallback_out: List[str] = []
    for bill in fallback_bills[:5]:
        bill_number = bill.get("bill_number", "").strip()
        bill_title = bill.get("bill_title", "").strip()
        bill_summary = bill.get("bill_summary", "").strip()

        if bill_number and bill_summary:
            if bill_title:
                fallback_out.append(f"{bill_number}::{bill_title} — {bill_summary}")
            else:
                fallback_out.append(f"{bill_number}::{bill_summary}")

    return " || ".join(fallback_out)


def to_sheet_row(
    legislator: str,
    result: Dict[str, Any],
    bills: List[Dict[str, str]],
) -> List[str]:
    committee_relevance_summary = str(result.get("committee_relevance_summary", "")).strip()
    time_in_office_summary = join_pipe(result.get("time_in_office_summary", []))
    generated_biography = join_pipe(result.get("generated_biography", []))
    key_issues = join_pipe(result.get("key_issues", []))
    district_development_signals = join_pipe(result.get("district_development_signals", []))
    legislative_focus_areas = join_pipe(result.get("legislative_focus_areas", []))
    key_bills = normalize_key_bills(result.get("key_bills", []), bills)
    political_positioning = str(result.get("political_positioning", "")).strip()
    political_positioning_bullets = join_pipe(result.get("political_positioning_bullets", []))
    sbdc_framing = str(result.get("sbdc_framing", "")).strip()
    talking_points = join_pipe(result.get("talking_points", []))
    bills_analyzed_count = str(len(bills))
    source_bill_numbers = " | ".join([b["bill_number"] for b in bills])
    last_updated = datetime.now(timezone.utc).isoformat()
    profile_processed = "TRUE"
    notes = ""

    return [
        legislator,                     # A
        committee_relevance_summary,   # B
        time_in_office_summary,        # C
        generated_biography,           # D
        key_issues,                    # E
        district_development_signals,  # F
        legislative_focus_areas,       # G
        key_bills,                     # H
        political_positioning,         # I
        political_positioning_bullets, # J
        sbdc_framing,                  # K
        talking_points,                # L
        bills_analyzed_count,          # M
        source_bill_numbers,           # N
        last_updated,                  # O
        profile_processed,             # P
        notes,                         # Q
    ]


def to_error_row(legislator: str, bills: List[Dict[str, str]], error: str) -> List[str]:
    return [
        legislator,                                    # A
        "",                                            # B
        "",                                            # C
        "",                                            # D
        "",                                            # E
        "",                                            # F
        "",                                            # G
        "",                                            # H
        "",                                            # I
        "",                                            # J
        "",                                            # K
        "",                                            # L
        str(len(bills)),                               # M
        " | ".join([b["bill_number"] for b in bills]), # N
        datetime.now(timezone.utc).isoformat(),        # O
        "FALSE",                                       # P
        error[:500],                                   # Q
    ]


def write_profile(service, existing_profiles: Dict[str, int], row_values: List[str]) -> None:
    legislator = row_values[0]

    if legislator in existing_profiles:
        row_num = existing_profiles[legislator]
        target_range = f"Profiles_Dynamic!A{row_num}:Q{row_num}"
        sheets_update_range(service, target_range, [row_values])
    else:
        sheets_append_rows(service, "Profiles_Dynamic!A:Q", [row_values])


# =========================
# Main
# =========================
def main():
    print(f"GEMINI_MODEL: {GEMINI_MODEL}")
    print(f"MAX_BILLS_PER_LEGISLATOR: {MAX_BILLS_PER_LEGISLATOR}")
    print(f"PROFILE_MAX_RETRIES: {PROFILE_MAX_RETRIES}")
    print(f"PROFILE_REQUEST_DELAY_SECONDS: {PROFILE_REQUEST_DELAY_SECONDS}")
    print(f"MIN_BILLS_REQUIRED: {MIN_BILLS_REQUIRED}")
    if ONLY_LEGISLATOR:
        print(f"ONLY_LEGISLATOR: {ONLY_LEGISLATOR}")

    service = get_sheets_service()
    client = build_client()

    metadata_by_legislator = load_metadata(service)
    activity_by_legislator = load_activity(service)
    existing_profiles = load_existing_profiles(service)

    legislators = sorted(set(metadata_by_legislator.keys()) & set(activity_by_legislator.keys()))

    if ONLY_LEGISLATOR:
        legislators = [name for name in legislators if name == ONLY_LEGISLATOR]

    print(f"Found {len(legislators)} legislator(s) with both metadata and processed bills.")

    for legislator in legislators:
        metadata = metadata_by_legislator[legislator]
        all_bills = activity_by_legislator[legislator]

        if len(all_bills) < MIN_BILLS_REQUIRED:
            print(f"Skipping {legislator}: not enough processed bills yet ({len(all_bills)}).")
            continue

        selected_bills = select_best_bills(all_bills, MAX_BILLS_PER_LEGISLATOR)

        print(f"Building profile for {legislator} using {len(selected_bills)} selected bill(s)...")
        for bill in selected_bills:
            print(f"  - {bill['bill_number']}")

        try:
            prompt = build_prompt(metadata, selected_bills)
            result = call_gemini(client, prompt)

            row_values = to_sheet_row(legislator, result, selected_bills)
            write_profile(service, existing_profiles, row_values)

            print(f"Updated profile: {legislator}")

            if PROFILE_REQUEST_DELAY_SECONDS > 0:
                time.sleep(PROFILE_REQUEST_DELAY_SECONDS)

        except Exception as exc:
            print(f"Failed profile for {legislator}: {exc}")
            row_values = to_error_row(legislator, selected_bills, f"Error: {exc}")
            write_profile(service, existing_profiles, row_values)


if __name__ == "__main__":
    main()
