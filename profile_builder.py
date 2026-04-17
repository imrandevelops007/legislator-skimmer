import os
import json
import time
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from google import genai


# =========================
# Config
# =========================

SHEET_ID = os.environ["SHEET_ID"]
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
MAX_BILLS_PER_LEGISLATOR = int(os.getenv("MAX_BILLS_PER_LEGISLATOR", "12"))
MIN_SUBSTANTIVE_BILLS_REQUIRED = int(os.getenv("MIN_SUBSTANTIVE_BILLS_REQUIRED", "4"))
MIN_TOTAL_ITEMS_REQUIRED = int(os.getenv("MIN_TOTAL_ITEMS_REQUIRED", "6"))
PROFILE_MAX_RETRIES = int(os.getenv("PROFILE_MAX_RETRIES", "2"))
PROFILE_REQUEST_DELAY_SECONDS = float(os.getenv("PROFILE_REQUEST_DELAY_SECONDS", "5"))
STOP_ON_QUOTA_EXHAUSTION = os.getenv("STOP_ON_QUOTA_EXHAUSTION", "true").strip().lower() == "true"
ONLY_LEGISLATOR = os.getenv("ONLY_LEGISLATOR", "").strip()

LEGISLATORS_TAB = "Legislators"
ACTIVITY_TAB = "Activity_Items"
METADATA_TAB = "Legislator_Metadata"
PROFILES_TAB = "Profiles_Dynamic"

LEGISLATORS_RANGE = f"{LEGISLATORS_TAB}!A1:Z"
ACTIVITY_RANGE = f"{ACTIVITY_TAB}!A1:I"
METADATA_RANGE = f"{METADATA_TAB}!A1:Z"
PROFILES_RANGE = f"{PROFILES_TAB}!A1:R"


# =========================
# Helpers
# =========================

def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def bool_from_cell(value: Any) -> bool:
    return clean(value).lower() in {"true", "1", "yes", "y"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_bill_prefix(bill_number: str) -> str:
    bill_number = clean(bill_number).upper()
    if not bill_number:
        return ""
    m = re.match(r"^([A-Z]+)", bill_number)
    return m.group(1) if m else ""


def looks_like_quota_error(text: str) -> bool:
    t = clean(text).lower()
    return (
        "resource_exhausted" in t
        or "quota exceeded" in t
        or "429" in t
        or "generate_content_free_tier_requests" in t
    )


def looks_like_unavailable_error(text: str) -> bool:
    t = clean(text).lower()
    return "503" in t or "unavailable" in t or "high demand" in t


def pad_row(row: List[str], length: int) -> List[str]:
    if len(row) < length:
        row = row + [""] * (length - len(row))
    return row[:length]


# =========================
# Google clients
# =========================

def get_creds():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    return Credentials.from_service_account_info(info, scopes=SCOPES)


def get_sheets_service():
    return build("sheets", "v4", credentials=get_creds())


def get_gemini_client():
    return genai.Client(api_key=GEMINI_API_KEY)


# =========================
# Sheets helpers
# =========================

def sheets_get(service, rng: str) -> List[List[str]]:
    return (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SHEET_ID, range=rng)
        .execute()
        .get("values", [])
    )


def sheets_update(service, rng: str, values: List[List[str]]) -> None:
    (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=SHEET_ID,
            range=rng,
            valueInputOption="RAW",
            body={"values": values},
        )
        .execute()
    )


# =========================
# Data loading
# =========================

def load_tab_as_dicts(service, rng: str) -> Tuple[List[str], List[Dict[str, str]]]:
    values = sheets_get(service, rng)
    if not values:
        return [], []

    headers = values[0]
    rows = []
    for raw in values[1:]:
        row = pad_row(raw, len(headers))
        rows.append({headers[i]: row[i] for i in range(len(headers))})
    return headers, rows


# =========================
# Ranking logic
# =========================

def item_priority(item: Dict[str, str]) -> Tuple[int, int]:
    bill_number = clean(item.get("Bill Number", ""))
    prefix = normalize_bill_prefix(bill_number)

    # lower number = higher priority
    if prefix in {"HB", "SB"}:
        tier = 1
    elif prefix in {"HJR", "SJR"}:
        tier = 2
    elif prefix in {"HR", "SR", "HCR", "SCR"}:
        tier = 3
    else:
        tier = 4

    # prefer rows that actually have a bill number and title
    detail_bonus = 0
    if clean(item.get("Bill Title", "")):
        detail_bonus -= 1
    if bill_number:
        detail_bonus -= 1

    return (tier, detail_bonus)


def select_best_items(items: List[Dict[str, str]], limit: int) -> List[Dict[str, str]]:
    ranked = sorted(items, key=item_priority)
    return ranked[:limit]


def split_items(items: List[Dict[str, str]]) -> Tuple[List[Dict[str, str]], List[Dict[str, str]], List[Dict[str, str]]]:
    substantive = []
    joint = []
    other = []

    for item in items:
        prefix = normalize_bill_prefix(item.get("Bill Number", ""))
        if prefix in {"HB", "SB"}:
            substantive.append(item)
        elif prefix in {"HJR", "SJR"}:
            joint.append(item)
        else:
            other.append(item)

    return substantive, joint, other


# =========================
# Prompt building
# =========================

def summarize_item_for_prompt(item: Dict[str, str]) -> str:
    bill_number = clean(item.get("Bill Number", ""))
    title = clean(item.get("Bill Title", ""))
    summary = clean(item.get("Bill Summary", ""))

    label = bill_number if bill_number else "No bill number"
    return f"- {label}: {title}\n  Summary: {summary}"


def build_profile_prompt(legislator: str, metadata: Dict[str, str], selected_items: List[Dict[str, str]]) -> str:
    metadata_lines = []
    for key, value in metadata.items():
        v = clean(value)
        if v:
            metadata_lines.append(f"{key}: {v}")

    items_text = "\n".join(summarize_item_for_prompt(item) for item in selected_items)

    return f"""
You are generating a structured legislator briefing profile for internal outreach use.

Requirements:
- Be factual and grounded.
- Use only the metadata and legislative items provided.
- Do not hallucinate.
- Keep it skimmable and concise.
- Prefer bullet-like phrasing.
- Avoid redundancy across sections.
- If legislative activity is mostly resolutions or joint resolutions, still generate a useful profile from those signals.
- Prioritize substantive work when present, but do not refuse to build a profile if only lower-signal legislative activity is available.

Return valid JSON only with this exact shape:
{{
  "Committee_Relevance_Summary": "...",
  "Time_In_Office_Summary": "...",
  "Generated_Biography": "...",
  "Key_Issues": "...",
  "District_Development_Signals": "...",
  "Legislative_Focus_Areas": "...",
  "Key_Bills": "...",
  "Political_Positioning": "...",
  "Political_Positioning_Bullets": "...",
  "SBDC_Framing": "...",
  "Talking_Points": "..."
}}

Legislator:
{legislator}

Metadata:
{chr(10).join(metadata_lines)}

Legislative Items:
{items_text}
""".strip()


# =========================
# Gemini
# =========================

def call_gemini_with_retry(client, prompt: str) -> Dict[str, str]:
    last_error = None

    for attempt in range(1, PROFILE_MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
            )
            text = clean(getattr(response, "text", ""))

            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?", "", text.strip(), flags=re.IGNORECASE).strip()
                text = re.sub(r"```$", "", text.strip()).strip()

            return json.loads(text)

        except Exception as e:
            last_error = str(e)

            if looks_like_unavailable_error(last_error):
                print(f"Gemini temporarily unavailable (attempt {attempt}/{PROFILE_MAX_RETRIES}).")
            else:
                print(f"Gemini failed attempt {attempt}/{PROFILE_MAX_RETRIES}: {last_error}")

            if attempt < PROFILE_MAX_RETRIES:
                time.sleep(PROFILE_REQUEST_DELAY_SECONDS)

    raise RuntimeError(f"Gemini failed after {PROFILE_MAX_RETRIES} retries: {last_error}")


# =========================
# Main
# =========================

def main():
    print("Connecting to Google Sheets and Gemini...")
    sheets_service = get_sheets_service()
    gemini_client = get_gemini_client()

    print("Loading tabs...")
    _, legislators_rows = load_tab_as_dicts(sheets_service, LEGISLATORS_RANGE)
    _, activity_rows = load_tab_as_dicts(sheets_service, ACTIVITY_RANGE)
    _, metadata_rows = load_tab_as_dicts(sheets_service, METADATA_RANGE)
    profile_headers, profiles_rows = load_tab_as_dicts(sheets_service, PROFILES_RANGE)

    metadata_by_legislator = {
        clean(row.get("Legislator", "")): row
        for row in metadata_rows
        if clean(row.get("Legislator", ""))
    }

    profiles_by_legislator = {
        clean(row.get("Legislator", "")): row
        for row in profiles_rows
        if clean(row.get("Legislator", ""))
    }

    if ONLY_LEGISLATOR:
        legislators_to_evaluate = [ONLY_LEGISLATOR]
    else:
        legislators_to_evaluate = []
        seen = set()

    for row in profiles_rows:
        legislator = clean(row.get("Legislator", ""))
        if not legislator or legislator in seen:
            continue

        profile_processed = bool_from_cell(row.get("Profile_Processed", ""))
        needs_rebuild = bool_from_cell(row.get("Needs_Rebuild", ""))

        if (not profile_processed) or needs_rebuild:
            legislators_to_evaluate.append(legislator)
            seen.add(legislator)

    print(f"Legislators to evaluate: {len(legislators_to_evaluate)}")

    for legislator in legislators_to_evaluate:
        print(f"Evaluating {legislator}")

        profile_row = profiles_by_legislator.get(legislator, {})
        profile_processed = bool_from_cell(profile_row.get("Profile_Processed", ""))
        needs_rebuild = bool_from_cell(profile_row.get("Needs_Rebuild", ""))

        if profile_processed and not needs_rebuild:
            print(f"Skipping {legislator}: profile already processed and no rebuild needed.")
            continue

        processed_items = [
            row for row in activity_rows
            if clean(row.get("Legislator", "")) == legislator
            and bool_from_cell(row.get("Processed", ""))
        ]

        if not processed_items:
            print(f"Skipping {legislator}: no processed legislative items available.")
            continue

        substantive, joint, other = split_items(processed_items)

        if len(processed_items) < MIN_TOTAL_ITEMS_REQUIRED:
            print(
                f"Skipping {legislator}: only {len(processed_items)} processed items available, "
                f"minimum required is {MIN_TOTAL_ITEMS_REQUIRED}."
            )
            continue

        selected_items = select_best_items(processed_items, MAX_BILLS_PER_LEGISLATOR)

        metadata = metadata_by_legislator.get(legislator, {})
        prompt = build_profile_prompt(legislator, metadata, selected_items)

        try:
            result = call_gemini_with_retry(gemini_client, prompt)
        except Exception as e:
            error_text = str(e)
            print(f"Profile build failed for {legislator}: {error_text}")
            if STOP_ON_QUOTA_EXHAUSTION and looks_like_quota_error(error_text):
                break
            continue

        source_bill_numbers = " | ".join(
            clean(item.get("Bill Number", "")) or clean(item.get("Bill Title", ""))[:60]
            for item in selected_items
        )

        output_row = [
            legislator,
            clean(result.get("Committee_Relevance_Summary", "")),
            clean(result.get("Time_In_Office_Summary", "")),
            clean(result.get("Generated_Biography", "")),
            clean(result.get("Key_Issues", "")),
            clean(result.get("District_Development_Signals", "")),
            clean(result.get("Legislative_Focus_Areas", "")),
            clean(result.get("Key_Bills", "")),
            clean(result.get("Political_Positioning", "")),
            clean(result.get("Political_Positioning_Bullets", "")),
            clean(result.get("SBDC_Framing", "")),
            clean(result.get("Talking_Points", "")),
            str(len(selected_items)),
            source_bill_numbers,
            now_iso(),
            "TRUE",
            clean(profile_row.get("Notes", "")),
            "FALSE",
        ]

        existing_index = None
        for i, row in enumerate(profiles_rows, start=2):
            if clean(row.get("Legislator", "")) == legislator:
                existing_index = i
                break

        if existing_index is not None:
            sheets_update(
                sheets_service,
                f"{PROFILES_TAB}!A{existing_index}:R{existing_index}",
                [output_row],
            )
        else:
            append_row_index = len(profiles_rows) + 2
            sheets_update(
                sheets_service,
                f"{PROFILES_TAB}!A{append_row_index}:R{append_row_index}",
                [output_row],
            )

        print(
            f"Built profile for {legislator} using "
            f"{len(selected_items)} processed items "
            f"({len(substantive)} substantive, {len(joint)} joint, {len(other)} other)."
        )

        time.sleep(PROFILE_REQUEST_DELAY_SECONDS)

    print("Profile builder complete.")


if __name__ == "__main__":
    main()
