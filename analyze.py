import os
import json
import time
import re
from typing import List, Tuple

import requests
from bs4 import BeautifulSoup

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from google import genai


# =========================
# Config
# =========================
SHEET_ID = os.environ["SHEET_ID"]
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Toggle AI calls without changing code:
#   DRY_RUN=true  -> no Gemini calls (fills placeholders, keeps Processed=FALSE)
#   DRY_RUN=false -> calls Gemini and marks Processed=TRUE
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# Gemini model
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# How many unprocessed rows to handle per run
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "10"))

# Page text size cap sent to Gemini
MAX_PAGE_CHARS = int(os.getenv("MAX_PAGE_CHARS", "12000"))

# Optional retry count
GEMINI_MAX_RETRIES = int(os.getenv("GEMINI_MAX_RETRIES", "3"))

# Activity_Items columns:
# A: URL
# B: Legislator
# C: Type
# D: Timestamp
# E: Bill Number
# F: Bill Title
# G: Bill Summary
# H: Processed
# I: Notes
ACTIVITY_RANGE = "Activity_Items!A2:I"


# =========================
# Google Sheets helpers
# =========================
def get_sheets_service():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)


def sheets_get_values(service, rng: str):
    return (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SHEET_ID, range=rng)
        .execute()
        .get("values", [])
    )


def sheets_update_row(service, row_number: int, values: List[str]):
    """
    Updates columns E-I for a given sheet row number.
    """
    range_name = f"Activity_Items!E{row_number}:I{row_number}"
    body = {"values": [values]}

    service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=range_name,
        valueInputOption="RAW",
        body=body,
    ).execute()


# =========================
# Helpers
# =========================
def pad_row(row: List[str], target_len: int = 9) -> List[str]:
    return row + [""] * (target_len - len(row))


def extract_bill_number_from_url(url: str) -> str:
    """
    Example:
    https://www.legislature.mi.gov/Home/GetObject?objectName=2025-HB-4102
    -> HB 4102
    """
    match = re.search(r"objectName=\d{4}-([A-Z]{1,3})-(\d+)", url, flags=re.IGNORECASE)
    if not match:
        return ""
    chamber = match.group(1).upper()
    number = match.group(2)
    return f"{chamber} {number}"


def clean_title_remove_bill_number(title: str, bill_number: str) -> str:
    if not title:
        return title

    cleaned = title

    if bill_number:
        cleaned = cleaned.replace(bill_number, "")

    # Remove leading patterns like "HB 4102 - ", "SB 12: ", "SR 5 – "
    cleaned = re.sub(r'^[A-Z]{1,3}\s*\d+\s*[-:–]\s*', '', cleaned)

    # Remove standalone leading bill numbers like "HB 4102 "
    cleaned = re.sub(r'^[A-Z]{1,3}\s*\d+\s*', '', cleaned)

    # Remove parenthetical bill number mentions like "(HB 4102)"
    cleaned = re.sub(r'\(\s*[A-Z]{1,3}\s*\d+\s*\)', '', cleaned)

    return cleaned.strip(" -:–")


# =========================
# Page Fetching
# =========================
def fetch_readable_text(url: str, max_chars: int = MAX_PAGE_CHARS) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; SBDC-Analyzer/1.0)"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.splitlines()]
    text = "\n".join([ln for ln in lines if ln])

    return text[:max_chars]


# =========================
# Gemini
# =========================
def _build_prompt(page_text: str, url: str, legislator_name: str, bill_number: str) -> str:
    return f"""
Return JSON only. No extra text.

Return:
{{
  "bill_title": "...",
  "bill_summary": "..."
}}

Rules:
- bill_title should be the official or closest clear bill title from the page.
- DO NOT include the bill number in the title.
- The title should be clean and readable on its own.
- bill_summary must be 2-4 plain English sentences.
- Keep bill_summary factual and readable.
- Do not invent details that are not supported by the page text.
- If the page text is limited, do your best with what is available.

URL: {url}
Legislator: {legislator_name}
Bill Number from URL: {bill_number}

PAGE TEXT:
{page_text}
""".strip()


def _safe_extract_json(raw: str) -> dict:
    raw = (raw or "").strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("Gemini did not return valid JSON.")
    return json.loads(raw[start:end + 1])


def gemini_analyze(page_text: str, url: str, legislator_name: str, bill_number: str) -> Tuple[str, str]:
    """
    Returns (bill_title, bill_summary).
    Handles retries for transient errors and prints clear logs.
    """
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is missing. Set it in GitHub Secrets or your local env.")

    client = genai.Client(api_key=api_key)
    prompt = _build_prompt(page_text, url, legislator_name, bill_number)

    last_err = None
    for attempt in range(1, GEMINI_MAX_RETRIES + 1):
        try:
            resp = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
            )
            data = _safe_extract_json(getattr(resp, "text", ""))

            bill_title = str(data.get("bill_title", "")).strip()
            bill_summary = str(data.get("bill_summary", "")).strip()

            if not bill_title or not bill_summary:
                raise ValueError("Gemini JSON missing bill_title or bill_summary.")

            return bill_title, bill_summary

        except Exception as e:
            last_err = e
            msg = str(e)

            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                wait_s = min(30, 2 * attempt)
                print(f"Gemini rate limited. Waiting {wait_s}s then retrying (attempt {attempt}/{GEMINI_MAX_RETRIES})...")
                time.sleep(wait_s)
                continue

            if "503" in msg or "UNAVAILABLE" in msg:
                wait_s = min(30, 2 * attempt)
                print(f"Gemini unavailable. Waiting {wait_s}s then retrying (attempt {attempt}/{GEMINI_MAX_RETRIES})...")
                time.sleep(wait_s)
                continue

            raise

    raise RuntimeError(f"Gemini failed after {GEMINI_MAX_RETRIES} retries: {last_err}")


# =========================
# Main
# =========================
def main():
    print(f"DRY_RUN mode: {DRY_RUN}")
    print(f"BATCH_SIZE: {BATCH_SIZE}")
    print(f"GEMINI_MODEL: {GEMINI_MODEL}")
    print(f"MAX_PAGE_CHARS: {MAX_PAGE_CHARS}")

    service = get_sheets_service()
    rows = sheets_get_values(service, ACTIVITY_RANGE)

    print(f"Loaded {len(rows)} activity rows.")

    processed_count = 0
    skipped_count = 0
    error_count = 0

    for idx, raw_row in enumerate(rows):
        if processed_count >= BATCH_SIZE:
            break

        # A2 is sheet row 2
        sheet_row_number = idx + 2
        row = pad_row(raw_row, 9)

        url = row[0].strip()
        legislator_name = row[1].strip()
        processed_flag_existing = row[7].strip().upper()
        notes_existing = row[8].strip().upper()

        if not url:
            skipped_count += 1
            continue

        # Always skip truly processed rows
        if processed_flag_existing == "TRUE":
            skipped_count += 1
            continue

        # During DRY_RUN, skip rows already marked as DRY RUN so they are not rewritten every time
        if DRY_RUN and notes_existing == "DRY RUN":
            skipped_count += 1
            continue

        print(f"\nAnalyzing row {sheet_row_number}: {url}")

        try:
            bill_number = extract_bill_number_from_url(url)
            page_text = fetch_readable_text(url, max_chars=MAX_PAGE_CHARS)

            if DRY_RUN:
                new_title = "DRY RUN TITLE"
                new_summary = "DRY RUN: Placeholder summary to verify sheet updates and pipeline flow."
                notes = "DRY RUN"
                processed_flag_to_write = "FALSE"
                print("DRY RUN: Skipping Gemini call.")
            else:
                new_title, new_summary = gemini_analyze(
                    page_text=page_text,
                    url=url,
                    legislator_name=legislator_name,
                    bill_number=bill_number,
                )
                new_title = clean_title_remove_bill_number(new_title, bill_number)
                notes = ""
                processed_flag_to_write = "TRUE"

            sheets_update_row(
                service,
                sheet_row_number,
                [
                    bill_number,              # E Bill Number
                    new_title,                # F Bill Title
                    new_summary,              # G Bill Summary
                    processed_flag_to_write,  # H Processed
                    notes,                    # I Notes
                ],
            )

            print("Updated successfully.")
            processed_count += 1

        except Exception as e:
            err_msg = str(e)[:400]
            print(f"Failed: {err_msg}")

            try:
                sheets_update_row(
                    service,
                    sheet_row_number,
                    [
                        row[4] if len(row) > 4 else "",   # existing Bill Number
                        row[5] if len(row) > 5 else "",   # existing Bill Title
                        row[6] if len(row) > 6 else "",   # existing Bill Summary
                        "FALSE",                          # Processed
                        f"Error: {err_msg}",              # Notes
                    ],
                )
            except Exception as update_err:
                print(f"Also failed to write error note back to sheet: {update_err}")

            error_count += 1

    print(
        f"\nDone. Processed={processed_count}, "
        f"Skipped={skipped_count}, Errors={error_count}"
    )


if __name__ == "__main__":
    main()
