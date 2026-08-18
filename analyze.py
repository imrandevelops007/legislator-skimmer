import json
import os
import re
import time
import requests
from google import genai
from google.genai import errors
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# Configuration loaded from environment variables
SHEET_ID = os.getenv("SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "5"))
MAX_ITEMS_PER_RUN = int(os.getenv("MAX_ITEMS_PER_RUN", "15"))
GEMINI_MAX_RETRIES = int(os.getenv("GEMINI_MAX_RETRIES", "2"))
REQUEST_DELAY_SECONDS = float(os.getenv("REQUEST_DELAY_SECONDS", "12.0"))
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

def get_sheets_service():
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON environment variable is missing.")
    if os.path.exists(GOOGLE_SERVICE_ACCOUNT_JSON):
        creds = Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_JSON, scopes=SCOPES)
    else:
        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)

def init_gemini_client():
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY environment variable is missing.")
    return genai.Client(api_key=GEMINI_API_KEY)

def extract_bill_from_url(url):
    """Fallback helper to pull bill number directly from URL if website fetch times out."""
    match = re.search(r"objectName=\d+-([A-Z]+)-(\d+)", url, re.IGNORECASE)
    if match:
        bill_type = match.group(1).upper()
        bill_num = match.group(2).lstrip("0")
        return f"{bill_type} {bill_num}"
    return ""

def call_gemini_with_retry(client, prompt, model_name=GEMINI_MODEL, max_retries=GEMINI_MAX_RETRIES):
    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            return response.text
        except errors.APIError as e:
            err_str = str(e)
            if "404" in err_str or "NOT_FOUND" in err_str or "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                print(f"API quota/availability issue ({e}). Halting Gemini retries.")
                raise e
            print(f"Gemini failed attempt {attempt}/{max_retries}: {e}")
            if attempt < max_retries:
                time.sleep(REQUEST_DELAY_SECONDS)
            else:
                raise e

def analyze_activity_item(client, url, legislator_name):
    # Fast extraction directly from URL
    fallback_bill = extract_bill_from_url(url)
    
    prompt = f"""
    Analyze the following Michigan legislative activity item URL for legislator {legislator_name}:
    URL: {url}
    Inferred Bill Number: {fallback_bill}
    
    Extract or confirm:
    1. "bill_number": (e.g. HB 4001, SB 0123)
    2. "bill_title": A brief title describing the bill.
    3. "bill_summary": A concise 2-sentence summary of the bill's objectives.

    Return ONLY a raw valid JSON object with keys: "bill_number", "bill_title", "bill_summary".
    Do not include markdown code block formatting.
    """
    response_text = call_gemini_with_retry(client, prompt)
    clean_text = response_text.replace("```json", "").replace("```", "").strip()
    return json.loads(clean_text)

def main():
    print(f"Initializing analyze.py with model {GEMINI_MODEL}...")
    service = get_sheets_service()
    client = init_gemini_client()

    result = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range="Activity_Items!A2:I"
    ).execute()
    rows = result.get("values", [])

    if not rows:
        print("No activity items found in Google Sheets.")
        return

    processed_count = 0

    for idx, row in enumerate(rows):
        if processed_count >= MAX_ITEMS_PER_RUN:
            print(f"Reached max items limit ({MAX_ITEMS_PER_RUN}). Stopping.")
            break

        # Check if Processed (Column H / index 7) is FALSE or empty
        processed_flag = row[7] if len(row) > 7 else "FALSE"
        if processed_flag.upper() == "TRUE":
            continue

        url = row[0] if len(row) > 0 else ""
        legislator = row[1] if len(row) > 1 else ""

        if not url or not legislator:
            continue

        print(f"Analyzing row {idx + 2}: {url} for {legislator}")
        try:
            analysis = analyze_activity_item(client, url, legislator)
            bill_num = analysis.get("bill_number", "") or extract_bill_from_url(url)
            bill_title = analysis.get("bill_title", "")
            bill_summary = analysis.get("bill_summary", "")

            # Ensure row has enough elements
            while len(row) < 9:
                row.append("")

            row[4] = bill_num
            row[5] = bill_title
            row[6] = bill_summary
            row[7] = "TRUE"  # Mark as processed

            if not DRY_RUN:
                row_num = idx + 2
                service.spreadsheets().values().update(
                    spreadsheetId=SHEET_ID,
                    range=f"Activity_Items!A{row_num}:I{row_num}",
                    valueInputOption="RAW",
                    body={"values": [row]}
                ).execute()

            processed_count += 1
            print(f"Successfully processed row {idx + 2}: {bill_num}")
            time.sleep(REQUEST_DELAY_SECONDS)

        except Exception as e:
            print(f"Error processing row {idx + 2}: {e}")
            err_str = str(e)
            if "404" in err_str or "NOT_FOUND" in err_str or "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                print("Stopping analysis loop immediately due to model error or quota limits.")
                break

    print(f"Analysis complete. Enriched {processed_count} rows.")

if __name__ == "__main__":
    main()