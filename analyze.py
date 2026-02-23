import os
import json
import re
from datetime import datetime
from urllib.parse import urlparse

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

GEMINI_MODEL = "gemini-2.0-flash"
BATCH_SIZE = 10
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
    "Budget"
]

ACTIVITY_RANGE = "Activity_Items!A2:G"


# =========================
# Google Sheets helpers
# =========================
def get_sheets_service():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)


def sheets_get_values(service, rng):
    return (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SHEET_ID, range=rng)
        .execute()
        .get("values", [])
    )


def sheets_update_row(service, row_number, values):
    """
    Updates columns E-G for a given sheet row number.
    """
    range_name = f"Activity_Items!E{row_number}:G{row_number}"
    body = {"values": [values]}

    service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=range_name,
        valueInputOption="RAW",
        body=body
    ).execute()


# =========================
# Page Fetching
# =========================
def fetch_readable_text(url, max_chars=15000):
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; SBDC-Analyzer/1.0)"
    }
    r = requests.get(url, headers=headers, timeout=20)
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
def gemini_analyze(page_text, url, legislator_name):
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    prompt = f"""
Return JSON only. No extra text.

Choose issue_tags only from:
{POLICY_TAGS}

Return:
{{
  "title": "...",
  "summary": "...",
  "issue_tags": ["Tag1", "Tag2"]
}}

Rules:
- Title should include bill number if present.
- Summary must be 2–3 plain English sentences.
- issue_tags must be 1–3 items from allowed list.

URL: {url}
Legislator: {legislator_name}

PAGE TEXT:
{page_text}
"""

    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt
    )

    raw = (resp.text or "").strip()

    # Extract JSON block safely
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("Gemini did not return valid JSON")

    data = json.loads(raw[start:end+1])

    tags = data.get("issue_tags", [])
    tags = [t for t in tags if t in POLICY_TAGS][:3]

    return (
        data.get("title", "").strip(),
        data.get("summary", "").strip(),
        ", ".join(tags)
    )


# =========================
# Main
# =========================
def main():
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
        title = row[4] if len(row) > 4 else ""

        if not url or title.strip():
            continue  # Skip already processed

        print(f"\nAnalyzing row {sheet_row_number}: {url}")

        try:
            page_text = fetch_readable_text(url)
            new_title, new_summary, new_tags = gemini_analyze(
                page_text, url, legislator_name
            )

            sheets_update_row(
                service,
                sheet_row_number,
                [new_title, new_summary, new_tags]
            )

            print("Updated successfully.")
            processed += 1

        except Exception as e:
            print(f"Failed: {e}")

    print(f"\nProcessed {processed} rows.")


if __name__ == "__main__":
    main()
