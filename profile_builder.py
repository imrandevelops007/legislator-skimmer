import os
import json
import time
import random
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
REQUEST_DELAY_SECONDS = float(os.getenv("PROFILE_REQUEST_DELAY_SECONDS", "3"))
ONLY_LEGISLATOR = os.getenv("ONLY_LEGISLATOR", "").strip()

# Sheet ranges
# Legislator_Metadata columns:
# A Legislator
# B Chamber
# C District
# D Party
# E First_Elected_to_Current_Chamber
# F Current_Term_Start
# G Current_Term_End
# H Time_In_Office_Note
# I Education
# J Professional_Background
# K Government_Experience
# L Committee_Assignments
# M Key_Issues_Source
# N Political_Positioning_Source
# O Verification_Notes
METADATA_RANGE = "Legislator_Metadata!A2:O"

# Activity_Items columns:
# A URL
# B Legislator
# C Type
# D Timestamp
# E Bill Number
# F Bill Title
# G Bill Summary
# H Processed
# I Notes
ACTIVITY_RANGE = "Activity_Items!A2:I"

# Profiles_Dynamic columns:
# A Legislator
# B Generated_Bio
# C Top_Themes
# D Key_Issues
# E Political_Positioning
# F Political_Position_Notes
# G Key_Bills
# H SBDC_Alignment
# I Talking_Points
# J Bills_Analyzed_Count
# K Source_Bill_Numbers
# L Last_Updated
# M Profile_Processed
# N Notes
PROFILES_RANGE = "Profiles_Dynamic!A2:N"


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
        row = pad_row(row, 15)
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

    # newest first
    for legislator in out:
        out[legislator].sort(key=lambda x: x["timestamp"], reverse=True)

    return out


def load_existing_profiles(service) -> Dict[str, int]:
    rows = sheets_get_values(service, PROFILES_RANGE)
    out: Dict[str, int] = {}

    for idx, row in enumerate(rows, start=2):
        row = pad_row(row, 14)
        legislator = row[0].strip()
        if legislator:
            out[legislator] = idx

    return out


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
You are a nonpartisan policy analyst helping create a concise legislator outreach briefing for the Michigan SBDC.

Return ONLY valid JSON with exactly these keys:
- generated_bio
- top_themes
- key_issues
- political_positioning
- political_position_notes
- key_bills
- sbdc_alignment
- talking_points

Rules:
- Use only facts supported by the metadata and bill list.
- Be concise, specific, and professional.
- generated_bio must be an array with 2 or 3 short bullet-ready statements.
- top_themes must be an array with 3 to 5 items.
- key_issues must be an array with 3 to 5 items.
- political_positioning must be a short label, not a paragraph.
- political_position_notes must be 1 or 2 sentences and cautious in tone.
- key_bills must be an array with 3 to 5 items. Each item must have:
  - bill_number
  - summary
- bill summaries must be one sentence each.
- sbdc_alignment must be 2 or 3 sentences.
- talking_points must be an array with 3 to 5 concise outreach bullets.
- Do not use markdown.
- Output JSON only.
""".strip()

    payload = {
        "metadata": metadata,
        "recent_bills": bills,
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
# Transform + write
# =========================
def to_sheet_row(
    legislator: str,
    result: Dict[str, Any],
    bills: List[Dict[str, str]],
) -> List[str]:
    generated_bio = " | ".join(result.get("generated_bio", []))
    top_themes = " | ".join(result.get("top_themes", []))
    key_issues = " | ".join(result.get("key_issues", []))
    political_positioning = str(result.get("political_positioning", "")).strip()
    political_position_notes = str(result.get("political_position_notes", "")).strip()

    key_bills = " || ".join(
        f'{str(item.get("bill_number", "")).strip()}::{str(item.get("summary", "")).strip()}'
        for item in result.get("key_bills", [])
    )

    sbdc_alignment = str(result.get("sbdc_alignment", "")).strip()
    talking_points = " | ".join(result.get("talking_points", []))
    bills_analyzed_count = str(len(bills))
    source_bill_numbers = " | ".join([b["bill_number"] for b in bills])
    last_updated = datetime.now(timezone.utc).isoformat()
    profile_processed = "TRUE"
    notes = ""

    return [
        legislator,
        generated_bio,
        top_themes,
        key_issues,
        political_positioning,
        political_position_notes,
        key_bills,
        sbdc_alignment,
        talking_points,
        bills_analyzed_count,
        source_bill_numbers,
        last_updated,
        profile_processed,
        notes,
    ]


def to_error_row(legislator: str, bills: List[Dict[str, str]], error: str) -> List[str]:
    return [
        legislator,
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        str(len(bills)),
        " | ".join([b["bill_number"] for b in bills]),
        datetime.now(timezone.utc).isoformat(),
        "FALSE",
        error[:500],
    ]


def write_profile(service, existing_profiles: Dict[str, int], row_values: List[str]) -> None:
    legislator = row_values[0]

    if legislator in existing_profiles:
        row_num = existing_profiles[legislator]
        target_range = f"Profiles_Dynamic!A{row_num}:N{row_num}"
        sheets_update_range(service, target_range, [row_values])
    else:
        sheets_append_rows(service, "Profiles_Dynamic!A:N", [row_values])


# =========================
# Main
# =========================
def main():
    print(f"GEMINI_MODEL: {GEMINI_MODEL}")
    print(f"MAX_BILLS_PER_LEGISLATOR: {MAX_BILLS_PER_LEGISLATOR}")
    print(f"PROFILE_MAX_RETRIES: {PROFILE_MAX_RETRIES}")
    print(f"PROFILE_REQUEST_DELAY_SECONDS: {REQUEST_DELAY_SECONDS}")
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
        bills = activity_by_legislator[legislator][:MAX_BILLS_PER_LEGISLATOR]

        if len(bills) < 3:
            print(f"Skipping {legislator}: not enough processed bills yet ({len(bills)}).")
            continue

        print(f"Building profile for {legislator} using {len(bills)} bill(s)...")

        try:
            prompt = build_prompt(metadata, bills)
            result = call_gemini(client, prompt)
            row_values = to_sheet_row(legislator, result, bills)
            write_profile(service, existing_profiles, row_values)
            print(f"Updated profile: {legislator}")

            if REQUEST_DELAY_SECONDS > 0:
                time.sleep(REQUEST_DELAY_SECONDS)

        except Exception as exc:
            print(f"Failed profile for {legislator}: {exc}")
            row_values = to_error_row(legislator, bills, f"Error: {exc}")
            write_profile(service, existing_profiles, row_values)


if __name__ == "__main__":
    main()
