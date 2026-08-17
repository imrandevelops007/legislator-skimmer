import os
import time
from google import genai
from google.genai import errors

# Configuration loaded from environment variables
SHEET_ID = os.getenv("SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Updated default model and fallback list
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
FALLBACK_GEMINI_MODELS = os.getenv(
    "FALLBACK_GEMINI_MODELS", 
    "gemini-3.5-flash,gemini-3.5-flash-lite"
).split(",")

MAX_BILLS_PER_LEGISLATOR = int(os.getenv("MAX_BILLS_PER_LEGISLATOR", "12"))
MIN_SUBSTANTIVE_BILLS_REQUIRED = int(os.getenv("MIN_SUBSTANTIVE_BILLS_REQUIRED", "4"))
MIN_TOTAL_ITEMS_REQUIRED = int(os.getenv("MIN_TOTAL_ITEMS_REQUIRED", "6"))
PROFILE_MAX_RETRIES = int(os.getenv("PROFILE_MAX_RETRIES", "3"))
PROFILE_REQUEST_DELAY_SECONDS = float(os.getenv("PROFILE_REQUEST_DELAY_SECONDS", "15.0"))
STOP_ON_QUOTA_EXHAUSTION = os.getenv("STOP_ON_QUOTA_EXHAUSTION", "true").lower() == "true"
ONLY_LEGISLATOR = os.getenv("ONLY_LEGISLATOR", "").strip()


def init_gemini_client():
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY environment variable is missing.")
    return genai.Client(api_key=GEMINI_API_KEY)


def generate_profile_with_fallbacks(client, prompt):
    models_to_try = [GEMINI_MODEL] + [m.strip() for m in FALLBACK_GEMINI_MODELS if m.strip()]
    
    for model_name in models_to_try:
        try:
            print(f"Attempting profile generation with model: {model_name}")
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            return response.text
        except errors.APIError as e:
            print(f"Failed with model {model_name}: {e}")
            time.sleep(PROFILE_REQUEST_DELAY_SECONDS)
    
    raise RuntimeError("All configured Gemini models failed during profile generation.")


def build_profiles():
    print("Connecting to Google Sheets and Gemini...")
    client = init_gemini_client()
    print("Evaluating legislators for dynamic profile rebuilds...")

    # Evaluation loop for processing dynamic profiles
    print("Profile builder complete.")


if __name__ == "__main__":
    build_profiles()