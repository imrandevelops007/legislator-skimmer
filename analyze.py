import os
import json
import time
from datetime import datetime
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
#   DRY_RUN=true  -> no Gemini calls (fills placeholders)
#   DRY_RUN=false -> calls Gemini
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# Gemini model (keep as env override so you can change without code edits)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

# How many blank rows to process per run
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "10"))

# Page text size cap sent to Gemini (reduces token cost)
MAX_PAGE_CHARS = int(os.getenv("MAX_PAGE_CHARS", "12000"))

# Optional: if Gemini rate limits, wait and retry
GEMINI_MAX_RETRIES = int(os.getenv("GEMINI_MAX_RETRIES", "3"))

POLICY_TAGS = [
    "Infrastructure",
    "Small Business",
    "Workforce",
    "Tax",
    "Education",
    "Healthcare",
    "Housing",
    "Energy",
    "Environment",
    "Agriculture",
    "Public Safety",
    "Government Operations",
    "Trade",
    "Technology",
    "Budget",
]

ACTIVITY_RANGE = "Activity_Items!A2:G"


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
    Updates columns E-G for a given sheet row number.
    """
    range_name = f"Activity_Items!E{row_number}:G{row_number}"
    body = {"values": [values]}

    service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=range_name,
        valueInputOption="RAW",
        body=body,
    ).execute()


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

    # Hard cap to reduce tokens/cost
    return text[:max_chars]


# =========================
# Gemini
# =========================
def _build_prompt(page_text: str, url: str, legislator_name: str) -> str:
    allowed = ", ".join(POLICY_TAGS)
    return f"""
Return JSON only. No extra text.

Allowed issue_tags (choose 1–3 from this list only):
[{allowed}]

Return:
{{
  "title": "...",
  "summary": "...",
  "issue_tags": ["Tag1", "Tag2"]
}}

Rules:
- Title should include bill number if present (example: "HB 4102 – ...").
- Summary must be 2–3 plain English sentences.
- issue_tags must be 1–3 items from the allowed list only.

URL: {url}
Legislator: {legislator_name}

PAGE TEXT:
{page_text}
""".strip()


def _safe_extract_json(raw: str) -> dict:
    raw = (raw or "").strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("Gemini did not return valid JSON.")
    return json.loads(raw[start : end + 1])


def _normalize_tags(tags) -> str:
    if not isinstance(tags, list):
        tags = []
    clean = [t for t in tags if isinstance(t, str) and t in POLICY_TAGS][:3]
    return ", ".join(clean)


def gemini_analyze(page_text: str, url: str, legislator_name: str) -> Tuple[str, str, str]:
    """
    Returns (title, summary, issue_tags_csv).
    Handles retries for transient errors and prints clear logs.
    """
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is missing. Set it in GitHub Secrets or your local env.")

    client = genai.Client(api_key=api_key)
    prompt = _build_prompt(page_text, url, legislator_name)

    last_err = None
    for attempt in range(1, GEMINI_MAX_RETRIES + 1):
        try:
            resp = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
            )
            data = _safe_extract_json(getattr(resp, "text", ""))

            title = str(data.get("title", "")).strip()
            summary = str(data.get("summary", "")).strip()
            tags_csv = _normalize_tags(data.get("issue_tags", []))

            if not title or not summary:
                raise ValueError("Gemini JSON missing title/summary.")

            return title, summary, tags_csv

        except Exception as e:
            last_err = e
            msg = str(e)
            # If rate limited, back off a bit
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                wait_s = min(30, 2 * attempt)
                print(f"Gemini rate limited. Waiting {wait_s}s then retrying (attempt {attempt}/{GEMINI_MAX_RETRIES})...")
                time.sleep(wait_s)
                continue
            # Transient service issues
            if "503" in msg or "UNAVAILABLE" in msg:
                wait_s = min(30, 2 * attempt)
                print(f"Gemini unavailable. Waiting {wait_s}s then retrying (attempt {attempt}/{GEMINI_MAX_RETRIES})...")
                time.sleep(wait_s)
                continue
            # Otherwise fail fast
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
    processed = 0

    for idx, row in enumerate(rows):
        if processed >= BATCH_SIZE:
            break

        # Sheet row number (A2 is row 2)
        sheet_row_number = idx + 2

        url = row[0] if len(row) > 0 else ""
        legislator_name = row[1] if len(row) > 1 else ""
        existing_title = row[4] if len(row) > 4 else ""

        # Only process blank title rows
        if not url or str(existing_title).strip():
            continue

        print(f"\nAnalyzing row {sheet_row_number}: {url}")

        try:
            page_text = fetch_readable_text(url, max_chars=MAX_PAGE_CHARS)

            if DRY_RUN:
                # Placeholders so you can verify sheet updates and flow without spending tokens
                new_title = "DRY RUN (Gemini disabled)"
                new_summary = "DRY RUN: This is a placeholder summary while Gemini is disabled."
                new_tags = "DRY RUN"
                print("DRY RUN: Skipping Gemini call.")
            else:
                new_title, new_summary, new_tags = gemini_analyze(page_text, url, legislator_name)

            sheets_update_row(service, sheet_row_number, [new_title, new_summary, new_tags])
            print("Updated successfully.")
            processed += 1

        except Exception as e:
            print(f"Failed: {e}")

    print(f"\nProcessed {processed} rows.")


if __name__ == "__main__":
    main()
