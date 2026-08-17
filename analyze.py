import os
import json
import re
import time
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse, parse_qs

import requests
import urllib3
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from google import genai

# Suppress SSL insecure request warnings when verify=False is used
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# =========================
# Config
# =========================
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

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
STOP_ON_QUOTA_EXHAUSTION = os.getenv("STOP_ON_QUOTA_EXHAUSTION", "true").lower() == "true"
ONLY_LEGISLATOR = os.getenv("ONLY_LEGISLATOR", "").strip()

# New repair toggle
REPAIR_MISSING_BILL_NUMBERS = os.getenv("REPAIR_MISSING_BILL_NUMBERS", "true").lower() == "true"

ACTIVITY_TAB = "Activity_Items"
PROFILES_TAB = "Profiles_Dynamic"

EXPECTED_ACTIVITY_HEADERS = [
    "URL",
    "Legislator",
    "Type",
    "Timestamp",
    "Bill Number",
    "Bill Title",
    "Bill Summary",
    "Processed",
    "Notes",
]

EXPECTED_PROFILES_HEADERS = [
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
def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def bool_from_cell(value: Any) -> bool:
    return clean(value).lower() in {"true", "1", "yes", "y"}


def get_col_index(headers: List[str], name: str) -> int:
    try:
        return headers.index(name)
    except ValueError:
        raise RuntimeError(f"Missing required column: {name}")


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


def sheets_batch_update(service, data: List[Dict[str, Any]]) -> None:
    if not data:
        return

    (
        service.spreadsheets()
        .values()
        .batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"valueInputOption": "RAW", "data": data},
        )
        .execute()
    )


# =========================
# Content extraction
# =========================
def fetch_page_text(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
        )
    }
    # verify=False handles SSL certificate verification issues on Michigan Legislature URLs
    response = requests.get(url, headers=headers, timeout=30, verify=False)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{2,}", "\n\n", text).strip()
    return text[:MAX_PAGE_CHARS]


# =========================
# Bill number extraction / normalization
# =========================
def extract_object_name_from_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        object_name = qs.get("objectName", [""])[0]
        return clean(object_name)
    except Exception:
        return ""


def normalize_bill_number_from_object_name(object_name: str) -> str:
    """
    Examples:
      2025-HB-4001 -> HB 4001
      2025-SR-0003 -> SR 0003
      2025-HJR-B   -> HJR B
      2025-SJR-F   -> SJR F
      2025-SCR-0010 -> SCR 0010
    """
    object_name = clean(object_name)
    if not object_name:
        return ""

    m = re.match(r"^\d{4}-([A-Z]+)-([A-Z0-9]+)$", object_name, flags=re.IGNORECASE)
    if not m:
        return ""

    prefix = m.group(1).upper()
    suffix = m.group(2).upper()

    return f"{prefix} {suffix}"


def extract_bill_number_from_text(text: str) -> str:
    text = clean(text)
    if not text:
        return ""

    patterns = [
        r"\b(HB|SB|HR|SR|HJR|SJR|HCR|SCR)\s+([A-Z]|\d{1,4})\b",
        r"\b(House Bill|Senate Bill|House Resolution|Senate Resolution|House Joint Resolution|Senate Joint Resolution|House Concurrent Resolution|Senate Concurrent Resolution)\s+([A-Z]|\d{1,4})\b",
    ]

    for pattern in patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            prefix = m.group(1)
            suffix = m.group(2).upper()

            prefix_map = {
                "house bill": "HB",
                "senate bill": "SB",
                "house resolution": "HR",
                "senate resolution": "SR",
                "house joint resolution": "HJR",
                "senate joint resolution": "SJR",
                "house concurrent resolution": "HCR",
                "senate concurrent resolution": "SCR",
            }

            normalized_prefix = prefix_map.get(prefix.lower(), prefix.upper())
            return f"{normalized_prefix} {suffix}"

    return ""


def choose_bill_number(url: str, page_text: str, title: str) -> str:
    object_name = extract_object_name_from_url(url)
    from_object_name = normalize_bill_number_from_object_name(object_name)
    if from_object_name:
        return from_object_name

    from_title = extract_bill_number_from_text(title)
    if from_title:
        return from_title

    from_page = extract_bill_number_from_text(page_text)
    if from_page:
        return from_page

    return ""


def strip_bill_number_prefix_from_title(title: str, bill_number: str) -> str:
    title = clean(title)
    bill_number = clean(bill_number)

    if not title:
        return ""
    if not bill_number:
        return title

    escaped_bill_number = re.escape(bill_number)
    patterns = [
        rf"^{escaped_bill_number}\s*[:\-–]\s*",
        rf"^{escaped_bill_number}\s+of\s+\d{{4}}\s*[:\-–]?\s*",
        rf"^(House Bill|Senate Bill|House Resolution|Senate Resolution|House Joint Resolution|Senate Joint Resolution|House Concurrent Resolution|Senate Concurrent Resolution)\s+([A-Z]|\d{{1,4}})\s*(of\s+\d{{4}})?\s*[:\-–]?\s*",
    ]

    cleaned = title
    for pattern in patterns:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE).strip()

    return cleaned or title


# =========================
# Gemini
# =========================
def build_prompt(page_text: str, url: str) -> str:
    return f"""
You are analyzing a Michigan legislative item page.

Your job:
1. Determine a clean bill title only.
2. Do NOT include the bill number in the title if it can be separated.
3. Produce a concise factual summary in 2-4 sentences.
4. Do not add speculation.
5. If the content is weak or ceremonial, still summarize it accurately.
6. Return valid JSON only.

JSON format:
{{
  "title": "...",
  "summary": "..."
}}

URL:
{url}

Page text:
{page_text}
""".strip()


def call_gemini_with_retry(client, prompt: str) -> Dict[str, str]:
    last_error = None

    for attempt in range(1, GEMINI_MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
            )
            text = clean(getattr(response, "text", ""))

            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?", "", text.strip(), flags=re.IGNORECASE).strip()
                text = re.sub(r"```$", "", text.strip()).strip()

            parsed = json.loads(text)
            return {
                "title": clean(parsed.get("title")),
                "summary": clean(parsed.get("summary")),
            }

        except Exception as e:
            last_error = str(e)

            if looks_like_unavailable_error(last_error):
                print(f"Gemini temporarily unavailable (attempt {attempt}/{GEMINI_MAX_RETRIES}).")
            else:
                print(f"Gemini failed attempt {attempt}/{GEMINI_MAX_RETRIES}: {last_error}")

            if attempt < GEMINI_MAX_RETRIES:
                wait_seconds = REQUEST_DELAY_SECONDS + 1
                print(f"Waiting {wait_seconds:.1f}s before retry...")
                time.sleep(wait_seconds)

    raise RuntimeError(f"Gemini failed after {GEMINI_MAX_RETRIES} retries: {last_error}")


# =========================
# Profile rebuild helpers
# =========================
def ensure_profiles_headers(service) -> List[str]:
    existing = sheets_get(service, f"{PROFILES_TAB}!1:1")
    if not existing:
        sheets_update(service, f"{PROFILES_TAB}!1:1", [EXPECTED_PROFILES_HEADERS])
        return EXPECTED_PROFILES_HEADERS

    headers = existing[0]
    if headers != EXPECTED_PROFILES_HEADERS:
        missing = [h for h in EXPECTED_PROFILES_HEADERS if h not in headers]
        if missing:
            headers = headers + missing
            sheets_update(service, f"{PROFILES_TAB}!1:1", [headers])
        return headers

    return headers


def load_profiles(service) -> Tuple[List[str], List[List[str]], Dict[str, int]]:
    headers = ensure_profiles_headers(service)
    rows = sheets_get(service, f"{PROFILES_TAB}!A2:R")
    normalized_rows = [row + [""] * (len(headers) - len(row)) for row in rows]

    legislator_idx = get_col_index(headers, "Legislator")
    profile_index: Dict[str, int] = {}

    for i, row in enumerate(normalized_rows):
        legislator = clean(row[legislator_idx])
        if legislator:
            profile_index[legislator] = i

    return headers, normalized_rows, profile_index


def mark_profile_needs_rebuild(
    service,
    profile_headers: List[str],
    profile_rows: List[List[str]],
    profile_index: Dict[str, int],
    legislator: str,
) -> None:
    legislator_idx = get_col_index(profile_headers, "Legislator")
    profile_processed_idx = get_col_index(profile_headers, "Profile_Processed")
    last_updated_idx = get_col_index(profile_headers, "Last_Updated")
    needs_rebuild_idx = get_col_index(profile_headers, "Needs_Rebuild")

    if legislator not in profile_index:
        new_row = [""] * len(profile_headers)
        new_row[legislator_idx] = legislator
        new_row[profile_processed_idx] = "FALSE"
        new_row[last_updated_idx] = ""
        new_row[needs_rebuild_idx] = "TRUE"

        profile_rows.append(new_row)
        profile_index[legislator] = len(profile_rows) - 1

        sheet_row = len(profile_rows) + 1
        sheets_update(service, f"{PROFILES_TAB}!A{sheet_row}:R{sheet_row}", [new_row])
        return

    row_idx = profile_index[legislator]
    sheet_row = row_idx + 2

    profile_rows[row_idx][needs_rebuild_idx] = "TRUE"
    sheets_update(service, f"{PROFILES_TAB}!R{sheet_row}", [["TRUE"]])


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
    print(f"ONLY_LEGISLATOR: {ONLY_LEGISLATOR or '(none)'}")
    print(f"REPAIR_MISSING_BILL_NUMBERS: {REPAIR_MISSING_BILL_NUMBERS}")

    sheets_service = get_sheets_service()
    gemini_client = get_gemini_client()

    header_rows = sheets_get(sheets_service, f"{ACTIVITY_TAB}!A1:I")
    if not header_rows:
        raise RuntimeError("Activity_Items is empty or missing headers.")

    headers = header_rows[0]
    if headers != EXPECTED_ACTIVITY_HEADERS:
        print("Warning: Activity_Items headers differ from expected. Using actual sheet headers.")

    activity_rows = sheets_get(sheets_service, f"{ACTIVITY_TAB}!A2:I")
    activity_rows = [row + [""] * (len(headers) - len(row)) for row in activity_rows]
    print(f"Loaded {len(activity_rows)} activity rows.")

    url_idx = get_col_index(headers, "URL")
    legislator_idx = get_col_index(headers, "Legislator")
    bill_number_idx = get_col_index(headers, "Bill Number")
    title_idx = get_col_index(headers, "Bill Title")
    summary_idx = get_col_index(headers, "Bill Summary")
    processed_idx = get_col_index(headers, "Processed")

    eligible: List[Tuple[int, List[str]]] = []

    for zero_based_idx, row in enumerate(activity_rows):
        legislator = clean(row[legislator_idx])
        processed = bool_from_cell(row[processed_idx])
        bill_number = clean(row[bill_number_idx])

        if ONLY_LEGISLATOR and legislator != ONLY_LEGISLATOR:
            continue

        should_repair = REPAIR_MISSING_BILL_NUMBERS and processed and not bill_number

        if processed and not should_repair:
            continue

        eligible.append((zero_based_idx, row))

    print(f"Found {len(eligible)} eligible row(s) to analyze/repair.")

    to_process = eligible[:MAX_ITEMS_PER_RUN]

    profile_headers, profile_rows, profile_index = load_profiles(sheets_service)

    processed_count = 0
    skipped_count = max(0, len(activity_rows) - len(to_process))
    error_count = 0
    quota_stopped = False

    for zero_based_idx, row in to_process:
        row_number = zero_based_idx + 2
        url = clean(row[url_idx])
        legislator = clean(row[legislator_idx])
        existing_bill_number = clean(row[bill_number_idx])

        print(f"Analyzing row {row_number}: {url}")
        print(f"Legislator: {legislator}")

        try:
            if DRY_RUN:
                page_text = ""
                raw_title = clean(row[title_idx]) or "Dry Run Title"
                cleaned_summary = clean(row[summary_idx]) or "Dry run summary."
            else:
                page_text = fetch_page_text(url)
                prompt = build_prompt(page_text, url)
                result = call_gemini_with_retry(gemini_client, prompt)

                raw_title = result["title"] or clean(row[title_idx]) or "Untitled legislative item"
                cleaned_summary = result["summary"] or clean(row[summary_idx]) or "No summary available."

            derived_bill_number = choose_bill_number(url, page_text, raw_title) or existing_bill_number
            cleaned_title = strip_bill_number_prefix_from_title(raw_title, derived_bill_number)

            if not cleaned_title:
                cleaned_title = raw_title or "Untitled legislative item"

            update_values = [[
                derived_bill_number,
                cleaned_title,
                cleaned_summary,
                "TRUE",
            ]]
            sheets_update(
                sheets_service,
                f"{ACTIVITY_TAB}!E{row_number}:H{row_number}",
                update_values,
            )

            mark_profile_needs_rebuild(
                sheets_service,
                profile_headers,
                profile_rows,
                profile_index,
                legislator,
            )

            processed_count += 1
            print(
                f"Success: {legislator} row {row_number} enriched. "
                f"Bill Number='{derived_bill_number}', Title='{cleaned_title}'"
            )

            time.sleep(REQUEST_DELAY_SECONDS)

        except Exception as e:
            error_text = str(e)
            error_count += 1

            if looks_like_quota_error(error_text):
                print(f"Failed: {error_text}")
                if STOP_ON_QUOTA_EXHAUSTION:
                    quota_stopped = True
                    break
            else:
                print(f"Failed: {error_text}")

    print(
        f"Done. Processed={processed_count}, "
        f"Skipped={skipped_count}, Errors={error_count}, "
        f"QuotaStopped={quota_stopped}"
    )


if __name__ == "__main__":
    main()