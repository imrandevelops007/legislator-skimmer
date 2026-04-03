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


def bill_is_substantive(bill_number: str) -> bool:
    bill_number = (bill_number or "").upper().strip()
    return bool(re.match(r"^(HB|SB|HCR|SCR)\s+\d+$", bill_number))


def bill_is_ceremonial(bill_number: str) -> bool:
    bill_number = (bill_number or "").upper().strip()
    return bool(re.match(r"^(HR|SR)\s+\d+$", bill_number))


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
    substantive_bills = [b for b in bills if bill_is_substantive(b.get("bill_number", ""))]
    ceremonial_bills = [b for b in bills if bill_is_ceremonial(b.get("bill_number", ""))]

    instructions = """
You are a nonpartisan policy analyst creating a one-page executive briefing for Michigan SBDC outreach.

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

PRIMARY GOAL:
Create a briefing that a director can skim in 20 to 30 seconds.

NON-NEGOTIABLE STYLE RULES:
- Output JSON only.
- Be concise.
- Prefer bullets over prose.
- No filler.
- No hedging unless uncertainty is unavoidable.
- Do not repeat the same fact across sections.
- Do not restate the same theme in slightly different wording.
- Strong signal matters more than exhaustive coverage.

BILL INTERPRETATION RULES:
- Substantive legislation should carry much more weight than ceremonial resolutions.
- Ceremonial or recognition resolutions such as HR and SR should NOT define the legislator's core policy focus.
- If only ceremonial items are available, include them only as weak supporting evidence.
- If only ceremonial items are available, prioritize committee roles, prior government experience, professional background, and durable issue cues over the resolution topics themselves.
- Do not let awareness days, commemorations, or honorary recognitions drive key issues, legislative focus, or political positioning unless there is no stronger evidence.
- When substantive bills exist, key_bills must primarily use those substantive bills.

COMMITTEE RELEVANCE RULES:
- String, not array
- Max 2 sentences
- Make it sharp and executive
- Lead with leadership roles if present
- Focus on practical relevance to regulation, economic development, funding, workforce, local government, infrastructure, agriculture, education, healthcare, or business climate
- Do not just list committees without explaining why they matter

TIME IN OFFICE RULES:
- Array of 2 to 3 bullets
- Timeline only
- No commentary

BIOGRAPHY RULES:
- Array of 2 to 3 bullets
- Education, profession, public service
- Tight and factual

KEY ISSUES RULES:
- Array of 3 to 5 bullets
- Format exactly: "Issue: explanation"
- Durable policy interests only
- If bill evidence is weak, infer mostly from committees, background, and governance roles

DISTRICT DEVELOPMENT SIGNALS RULES:
- Array of 2 to 4 bullets
- Concrete economic, workforce, infrastructure, agriculture, education, local government, or community-development implications only
- No speculative phrases like "could signal", "may suggest", or "appears to"

LEGISLATIVE FOCUS RULES:
- Array of 3 to 5 bullets
- Format exactly: "Focus Area: explanation"
- Reflect actual policy orientation, not ceremonial recognition topics
- If bill evidence is weak, rely more on committee and background signals

KEY BILLS RULES:
- Array of 3 to 5 objects
- Each object must contain:
  - bill_number
  - summary
- One sentence each
- Prefer HB/SB, then HCR/SCR
- HR/SR should only appear when substantive bills are unavailable
- If only ceremonial items are available, summaries should stay factual and brief and should not overstate policy significance

POLITICAL POSITIONING RULES:
- One short label only
- Format like:
  "Center-right | Pro-business | Budget-focused"
  "Center-left | Workforce-focused | Institutional"
- Do not just restate party

POLITICAL POSITIONING BULLETS RULES:
- Array of 2 to 3 bullets
- Focus on governing style and practical priorities
- Do not over-read ceremonial bills

SBDC FRAMING RULES:
- String, 2 to 3 short sentences max
- Must be specific to this legislator
- Tie the framing to the strongest concrete signals in the input
- Good anchors include:
  - regulatory relief
  - small business growth
  - rural entrepreneurship
  - workforce pipelines
  - local economic development
  - education-to-workforce pathways
  - agriculture or supply chain resilience
  - efficient local service delivery
- Avoid generic phrases that could fit almost anyone
- Explain what kind of business story, district outcome, or ROI framing is most likely to resonate with this legislator

TALKING POINTS RULES:
- Array of 4 to 5 bullets
- Each bullet must be short, actionable, and specific to the legislator
- Avoid vague outreach language

QUALITY FILTER:
Before finalizing, remove:
- repeated ideas
- generic outreach language
- ceremonial overemphasis
- long explanations
- broad statements not grounded in the inputs

Use only the metadata and bill list below. Do not invent facts.
""".strip()

    payload = {
        "metadata": metadata,
        "selected_recent_bills": bills,
        "signal_summary": {
            "selected_bill_count": len(bills),
            "substantive_bill_count": len(substantive_bills),
            "ceremonial_bill_count": len(ceremonial_bills),
            "has_substantive_bills": len(substantive_bills) > 0,
            "has_only_ceremonial_bills": len(substantive_bills) == 0 and len(ceremonial_bills) > 0,
        },
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
# Cleanup helpers
# =========================
def clean_bullet_text(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"\s+", " ", text)
    text = text.strip("•- ")
    return text


def clean_bullet_list(items: Any, max_items: int) -> List[str]:
    if not isinstance(items, list):
        return []

    cleaned: List[str] = []
    seen = set()

    for item in items:
        value = clean_bullet_text(str(item))
        if not value:
            continue

        lowered = value.lower()
        if lowered in seen:
            continue
        seen.add(lowered)

        cleaned.append(value)
        if len(cleaned) >= max_items:
            break

    return cleaned


def clean_summary_text(text: str, max_sentences: int = 2) -> str:
    text = clean_bullet_text(text)
    if not text:
        return ""

    parts = re.split(r"(?<=[.!?])\s+", text)
    parts = [p.strip() for p in parts if p.strip()]
    if not parts:
        return text

    return " ".join(parts[:max_sentences])


def normalize_committee_summary(text: str) -> str:
    text = clean_summary_text(text, max_sentences=2)
    text = re.sub(r"^As a member of\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^As a\s+", "", text, flags=re.IGNORECASE)
    return text.strip()


def normalize_sbdc_framing(text: str) -> str:
    text = clean_summary_text(text, max_sentences=3)

    replacements = {
        "Frame SBDC outreach around how services provide tangible local economic impact": "Frame SBDC outreach around tangible local economic impact",
        "Emphasize success stories that highlight": "Use success stories that highlight",
        "Discuss how SBDC services can help": "Show how SBDC support can help",
        "Highlight how SBDC services can help": "Show how SBDC services help",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    return text.strip()


# =========================
# Transform helpers
# =========================
def join_pipe(items: Any) -> str:
    if not isinstance(items, list):
        return ""
    return " | ".join([str(x).strip() for x in items if str(x).strip()])


def normalize_key_bills(items: Any, selected_bills: List[Dict[str, str]]) -> str:
    """
    Convert Gemini key_bills output into:
    BILL_NUMBER::summary || BILL_NUMBER::summary

    Fallback logic:
    1. Prefer substantive selected bills
    2. If none exist, allow ceremonial selected bills
    """
    substantive_selected = [b for b in selected_bills if bill_is_substantive(b.get("bill_number", ""))]
    ceremonial_selected = [b for b in selected_bills if bill_is_ceremonial(b.get("bill_number", ""))]

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

            summary = clean_summary_text(summary, max_sentences=1)

            if not bill_number or not summary:
                continue

            # If substantive selected bills exist, ignore ceremonial Gemini choices
            if substantive_selected and bill_is_ceremonial(bill_number):
                continue

            out.append(f"{bill_number}::{summary}")

    if out:
        return " || ".join(out[:5])

    fallback_source = substantive_selected if substantive_selected else ceremonial_selected if ceremonial_selected else selected_bills

    fallback_out: List[str] = []
    for bill in fallback_source[:5]:
        bill_number = bill.get("bill_number", "").strip()
        bill_title = clean_bullet_text(bill.get("bill_title", "").strip())
        bill_summary = clean_summary_text(bill.get("bill_summary", "").strip(), max_sentences=1)

        if not bill_number or not bill_summary:
            continue

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
    committee_relevance_summary = normalize_committee_summary(
        str(result.get("committee_relevance_summary", "")).strip()
    )

    time_in_office_summary = join_pipe(
        clean_bullet_list(result.get("time_in_office_summary", []), 3)
    )

    generated_biography = join_pipe(
        clean_bullet_list(result.get("generated_biography", []), 3)
    )

    key_issues = join_pipe(
        clean_bullet_list(result.get("key_issues", []), 5)
    )

    district_development_signals = join_pipe(
        clean_bullet_list(result.get("district_development_signals", []), 4)
    )

    legislative_focus_areas = join_pipe(
        clean_bullet_list(result.get("legislative_focus_areas", []), 5)
    )

    key_bills = normalize_key_bills(result.get("key_bills", []), bills)

    political_positioning = clean_bullet_text(
        str(result.get("political_positioning", "")).strip()
    )

    political_positioning_bullets = join_pipe(
        clean_bullet_list(result.get("political_positioning_bullets", []), 3)
    )

    sbdc_framing = normalize_sbdc_framing(
        str(result.get("sbdc_framing", "")).strip()
    )

    talking_points = join_pipe(
        clean_bullet_list(result.get("talking_points", []), 5)
    )

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

        substantive_count = sum(1 for b in selected_bills if bill_is_substantive(b.get("bill_number", "")))
        ceremonial_count = sum(1 for b in selected_bills if bill_is_ceremonial(b.get("bill_number", "")))

        print(f"Building profile for {legislator} using {len(selected_bills)} selected bill(s)...")
        print(f"  substantive selected: {substantive_count}")
        print(f"  ceremonial selected: {ceremonial_count}")
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
