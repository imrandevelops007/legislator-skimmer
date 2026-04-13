import os
import json
import re
import time
from typing import List, Dict, Any, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from google import genai


# =========================
# Config
# =========================

DRY_RUN = os.getenv("DRY_RUN", "false").strip().lower() == "true"

SHEET_ID = os.environ["SHEET_ID"]
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "3"))
MAX_ITEMS_PER_RUN = int(os.getenv("MAX_ITEMS_PER_RUN", str(BATCH_SIZE)))
MAX_PAGE_CHARS = int(os.getenv("MAX_PAGE_CHARS", "2500"))
GEMINI_MAX_RETRIES = int(os.getenv("GEMINI_MAX_RETRIES", "2"))
REQUEST_DELAY_SECONDS = float(os.getenv("REQUEST_DELAY_SECONDS", "7"))
STOP_ON_QUOTA_EXHAUSTION = os.getenv("STOP_ON_QUOTA_EXHAUSTION", "true").strip().lower() == "true"

ACTIVITY_RANGE = os.getenv("ACTIVITY_RANGE", "Activity_Items!A2:I")
PROFILES_TAB = os.getenv("PROFILES_TAB", "Profiles_Dynamic")

# Assumed Activity_Items columns:
# A URL
# B Legislator
# C Type
# D Timestamp
# E Bill Number
# F Bill Title
# G Bill Summary
# H Processed
# I Notes

ACTIVITY_HEADERS = [
    "URL",
    "Legislator",
    "Type",
    "Timestamp",
    "Bill_Number",
    "Bill_Title",
    "Bill_Summary",
    "Processed",
    "Notes",
]


# =========================
# Helpers
# =========================

def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def bool_from_cell(value: Any) -> bool:
    return clean(value).lower() in {"true", "yes", "1", "y"}


def normalize_header(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")


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


def chunk_text(text: str, limit: int) -> str:
    return clean(text)[:limit]


def extract_page_text(url: str) -> str:
    resp = requests.get(url, timeout=25)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    text = soup.get_text("\n", strip=True)
    return chunk_text(text, MAX_PAGE_CHARS)


def parse_json_response(text: str) -> Dict[str, Any]:
    text = clean(text)

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text.strip(), flags=re.IGNORECASE).strip()
        text = re.sub(r"```$", "", text.strip()).strip()

    return json.loads(text)


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

def sheets_get_values(service, rng: str) -> List[List[str]]:
    return (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SHEET_ID, range=rng)
        .execute()
        .get("values", [])
    )


def sheets_update_range(service, rng: str, values: List[List[str]]) -> None:
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


def sheets_batch_update(service, data: List[Dict[str, Any]]) -> None:
    if not data:
        return

    (
        service.spreadsheets()
        .values()
        .batchUpdate(
            spreadsheetId=SHEET_ID,
            body={
                "valueInputOption": "RAW",
                "data": data,
            },
        )
        .execute()
    )


def column_letter_from_index(index_1_based: int) -> str:
    result = ""
    while index_1_based > 0:
        index_1_based, rem = divmod(index_1_based - 1, 26)
        result = chr(65 + rem) + result
    return result


def get_tab_rows_with_headers(service, tab_name: str) -> Tuple[List[str], List[Dict[str, Any]]]:
    values = sheets_get_values(service, tab_name)
    if not values:
        return [], []

    raw_headers = values[0]
    norm_headers = [normalize_header(h) for h in raw_headers]

    rows = []
    for row_num, row in enumerate(values[1:], start=2):
        padded = row + [""] * (len(raw_headers) - len(row))
        item = {norm_headers[i]: padded[i] for i in range(len(raw_headers))}
        item["_row_number"] = row_num
        rows.append(item)

    return raw_headers, rows


def make_header_index(raw_headers: List[str]) -> Dict[str, int]:
    return {normalize_header(h): i + 1 for i, h in enumerate(raw_headers)}


def find_profile_row_for_legislator(profile_rows: List[Dict[str, Any]], legislator: str) -> Optional[Dict[str, Any]]:
    target = clean(legislator).lower()
    for row in profile_rows:
        if clean(row.get("legislator")).lower() == target:
            return row
    return None


def ensure_profiles_has_needs_rebuild(service) -> Tuple[List[str], Dict[str, int], List[Dict[str, Any]]]:
    raw_headers, rows = get_tab_rows_with_headers(service, PROFILES_TAB)

    if not raw_headers:
        raise RuntimeError(f"{PROFILES_TAB} is missing or empty. It must already exist with headers.")

    if "needs_rebuild" not in [normalize_header(h) for h in raw_headers]:
        updated_headers = raw_headers + ["Needs_Rebuild"]
        sheets_update_range(service, f"{PROFILES_TAB}!1:1", [updated_headers])
        raw_headers, rows = get_tab_rows_with_headers(service, PROFILES_TAB)

    return raw_headers, make_header_index(raw_headers), rows


def mark_legislator_needs_rebuild(
    service,
    legislator: str,
    profile_rows: List[Dict[str, Any]],
    profile_header_index: Dict[str, int],
) -> None:
    row = find_profile_row_for_legislator(profile_rows, legislator)
    if not row:
        return

    if "needs_rebuild" not in profile_header_index:
        return

    col_letter = column_letter_from_index(profile_header_index["needs_rebuild"])
    row_number = row["_row_number"]

    sheets_update_range(service, f"{PROFILES_TAB}!{col_letter}{row_number}", [["TRUE"]])


# =========================
# Activity loading
# =========================

def load_activity_rows(service) -> List[Dict[str, Any]]:
    rows = sheets_get_values(service, ACTIVITY_RANGE)
    out = []

    for idx, row in enumerate(rows, start=2):
        row = row + [""] * (len(ACTIVITY_HEADERS) - len(row))
        item = {ACTIVITY_HEADERS[i]: row[i] for i in range(len(ACTIVITY_HEADERS))}
        item["_row_number"] = idx
        out.append(item)

    return out


def eligible_activity_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    eligible = []
    for row in rows:
        if bool_from_cell(row.get("Processed")):
            continue
        if clean(row.get("Type")).lower() != "bill":
            continue
        eligible.append(row)
    return eligible


# =========================
# Gemini prompt
# =========================

def build_prompt(row: Dict[str, Any], page_text: str) -> str:
    legislator = clean(row.get("Legislator"))
    url = clean(row.get("URL"))
    bill_number = clean(row.get("Bill_Number"))

    return f"""
You are extracting structured bill information for a Michigan legislative intelligence system.

Return valid JSON only.
No markdown.
No code fences.

Use this shape:
{{
  "bill_number": "...",
  "bill_title": "...",
  "bill_summary": "..."
}}

Rules:
- bill_number should be the formal bill number if available, like HB 4001 or SB 12
- bill_title should be concise and cleaned
- bill_summary should be 1 to 3 sentences, factual, and useful for policy briefing
- do not invent facts
- if the bill number is already known, preserve it unless page text clearly shows a better formatted version

Context:
Legislator: {legislator}
Known bill number: {bill_number}
URL: {url}

Page text:
{page_text}
""".strip()


def call_gemini_with_retries(client, prompt: str) -> str:
    last_error = None

    for attempt in range(1, GEMINI_MAX_RETRIES + 1):
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

            if looks_like_unavailable_error(error_text):
                print(f"Gemini temporarily unavailable (attempt {attempt}/{GEMINI_MAX_RETRIES}).")
            elif looks_like_quota_error(error_text):
                print(f"Gemini quota/rate limit hit (attempt {attempt}/{GEMINI_MAX_RETRIES}).")
            else:
                print(f"Gemini failed (attempt {attempt}/{GEMINI_MAX_RETRIES}): {error_text}")

            if attempt < GEMINI_MAX_RETRIES:
                wait_seconds = REQUEST_DELAY_SECONDS + 1
                print(f"Waiting {wait_seconds:.1f}s before retry...")
                time.sleep(wait_seconds)

    raise RuntimeError(f"Gemini failed after {GEMINI_MAX_RETRIES} retries: {last_error}")


# =========================
# Main
# =========================

def main():
    print(f"DRY_RUN mode: {DRY_RUN}")
    print(f"BATCH_SIZE: {BATCH_SIZE}")
    print(f"MAX_ITEMS_PER_RUN: {MAX_ITEMS_PER_RUN}")
    print(f"GEMINI_MODEL: {GEMINI_MODEL}")
    print(f"MAX_PAGE_CHARS: {MAX_PAGE_CHARS}")
    print(f"GEMINI_MAX_RETRIES: {GEMINI_MAX_RETRIES}")
    print(f"REQUEST_DELAY_SECONDS: {REQUEST_DELAY_SECONDS}")
    print(f"STOP_ON_QUOTA_EXHAUSTION: {STOP_ON_QUOTA_EXHAUSTION}")

    sheets_service = get_sheets_service()
    gemini_client = get_gemini_client()

    activity_rows = load_activity_rows(sheets_service)
    print(f"Loaded {len(activity_rows)} activity rows.")

    raw_profile_headers, profile_header_index, profile_rows = ensure_profiles_has_needs_rebuild(sheets_service)

    eligible = eligible_activity_rows(activity_rows)
    print(f"Found {len(eligible)} eligible unprocessed row(s).")

    to_process = eligible[:MAX_ITEMS_PER_RUN]

    processed_count = 0
    skipped_count = 0
    error_count = 0
    quota_stopped = False

    for row in to_process:
        row_number = row["_row_number"]
        url = clean(row.get("URL"))
        legislator = clean(row.get("Legislator"))

        print(f"Analyzing row {row_number}: {url}")
        print(f"Legislator: {legislator}")

        try:
            if DRY_RUN:
                result = {
                    "bill_number": clean(row.get("Bill_Number")) or "UNKNOWN",
                    "bill_title": "Dry run title",
                    "bill_summary": "Dry run summary.",
                }
            else:
                page_text = extract_page_text(url)
                prompt = build_prompt(row, page_text)
                raw_text = call_gemini_with_retries(gemini_client, prompt)
                result = parse_json_response(raw_text)

            bill_number = clean(result.get("bill_number")) or clean(row.get("Bill_Number"))
            bill_title = clean(result.get("bill_title"))
            bill_summary = clean(result.get("bill_summary"))

            if not bill_title or not bill_summary:
                raise RuntimeError("Gemini returned incomplete structured bill data.")

            updates = [
                {"range": f"Activity_Items!E{row_number}", "values": [[bill_number]]},
                {"range": f"Activity_Items!F{row_number}", "values": [[bill_title]]},
                {"range": f"Activity_Items!G{row_number}", "values": [[bill_summary]]},
                {"range": f"Activity_Items!H{row_number}", "values": [["TRUE"]]},
                {"range": f"Activity_Items!I{row_number}", "values": [[""]]},
            ]
            sheets_batch_update(sheets_service, updates)

            # Mark profile for rebuild only after successful enrichment
            mark_legislator_needs_rebuild(
                sheets_service,
                legislator=legislator,
                profile_rows=profile_rows,
                profile_header_index=profile_header_index,
            )

            print(f"Success: {legislator} row {row_number} enriched.")
            processed_count += 1

            time.sleep(REQUEST_DELAY_SECONDS)

        except Exception as e:
            error_text = str(e)

            if looks_like_quota_error(error_text):
                print(f"Quota stop: {error_text}")
                error_count += 1
                quota_stopped = True

                if STOP_ON_QUOTA_EXHAUSTION:
                    print("Stopping run after quota exhaustion to avoid repeated failed calls.")
                    break

            else:
                print(f"Failed: {error_text}")
                error_count += 1

    skipped_count = len(activity_rows) - len(eligible)
    print(
        f"Done. Processed={processed_count}, "
        f"Skipped={skipped_count}, Errors={error_count}, QuotaStopped={quota_stopped}"
    )


if __name__ == "__main__":
    main()
