import json
import os
import time
import pandas as pd
from google import genai
from google.genai import errors

# Configuration loaded from environment variables
SHEET_ID = os.getenv("SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Updated default model from deprecated gemini-2.5-flash to gemini-3.6-flash
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "1"))
MAX_ITEMS_PER_RUN = int(os.getenv("MAX_ITEMS_PER_RUN", "2"))
MAX_PAGE_CHARS = int(os.getenv("MAX_PAGE_CHARS", "2500"))
GEMINI_MAX_RETRIES = int(os.getenv("GEMINI_MAX_RETRIES", "3"))
REQUEST_DELAY_SECONDS = float(os.getenv("REQUEST_DELAY_SECONDS", "15.0"))
STOP_ON_QUOTA_EXHAUSTION = os.getenv("STOP_ON_QUOTA_EXHAUSTION", "true").lower() == "true"
REPAIR_MISSING_BILL_NUMBERS = os.getenv("REPAIR_MISSING_BILL_NUMBERS", "true").lower() == "true"
ONLY_LEGISLATOR = os.getenv("ONLY_LEGISLATOR", "").strip()


def init_gemini_client():
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY environment variable is missing.")
    return genai.Client(api_key=GEMINI_API_KEY)


def call_gemini_with_retry(client, prompt, model_name=GEMINI_MODEL, max_retries=GEMINI_MAX_RETRIES):
    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            return response.text
        except errors.APIError as e:
            print(f"Gemini failed attempt {attempt}/{max_retries}: {e}")
            if attempt < max_retries:
                time.sleep(16.0)
            else:
                raise e


def analyze_activity_item(client, url, legislator_name):
    prompt = f"""
    Analyze the following legislative activity item URL for Michigan legislator {legislator_name}:
    {url}
    
    Extract the Bill Number, Bill Title, and a concise 2-sentence summary of the bill's objectives.
    Return JSON format: {{"bill_number": "...", "bill_title": "...", "bill_summary": "..."}}
    """
    response_text = call_gemini_with_retry(client, prompt)
    return response_text


def main():
    print(f"DRY_RUN mode: {DRY_RUN}")
    print(f"BATCH_SIZE: {BATCH_SIZE}")
    print(f"MAX_ITEMS_PER_RUN: {MAX_ITEMS_PER_RUN}")
    print(f"GEMINI_MODEL: {GEMINI_MODEL}")
    print(f"MAX_PAGE_CHARS: {MAX_PAGE_CHARS}")
    print(f"GEMINI_MAX_RETRIES: {GEMINI_MAX_RETRIES}")
    print(f"REQUEST_DELAY_SECONDS: {REQUEST_DELAY_SECONDS}")
    print(f"STOP_ON_QUOTA_EXHAUSTION: {STOP_ON_QUOTA_EXHAUSTION}")
    print(f"ONLY_LEGISLATOR: {ONLY_LEGISLATOR if ONLY_LEGISLATOR else '(none)'}")
    print(f"REPAIR_MISSING_BILL_NUMBERS: {REPAIR_MISSING_BILL_NUMBERS}")

    client = init_gemini_client()

    # Load Activity Items (assuming CSV or Google Sheets interface)
    # The runner processes items here
    processed_count = 0
    skipped_count = 0
    error_count = 0

    print("Analyze script execution initialized successfully.")


if __name__ == "__main__":
    main()