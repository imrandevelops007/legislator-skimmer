import json
import os
import time
from google import genai
from google.genai import errors
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SHEET_ID = os.getenv("SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
FALLBACK_GEMINI_MODELS = os.getenv("FALLBACK_GEMINI_MODELS", "gemini-1.5-flash").split(",")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

def get_sheets_service():
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON missing.")
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
            time.sleep(5)
    raise RuntimeError("All configured Gemini models failed during profile generation.")

def build_profiles():
    print("Connecting to Google Sheets and Gemini...")
    service = get_sheets_service()
    client = init_gemini_client()

    # Load Activity Items and Profiles
    activity_resp = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range="Activity_Items!A2:I"
    ).execute()
    activity_rows = activity_resp.get("values", [])

    profiles_resp = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range="Profiles_Dynamic!A1:R"
    ).execute()
    profile_data = profiles_resp.get("values", [])

    if not profile_data:
        print("No profiles header or rows found.")
        return

    headers = profile_data[0]
    profile_rows = profile_data[1:]

    # Group processed bills by legislator
    bills_by_legislator = {}
    for row in activity_rows:
        if len(row) > 7 and row[7].upper() == "TRUE":
            leg = row[1] if len(row) > 1 else ""
            summary = f"- Bill {row[4]}: {row[5]} ({row[6]})"
            bills_by_legislator.setdefault(leg, []).append(summary)

    print("Evaluating legislators for dynamic profile rebuilds...")

    for idx, p_row in enumerate(profile_rows):
        leg_name = p_row[0] if len(p_row) > 0 else ""
        needs_rebuild = p_row[17] if len(p_row) > 17 else "TRUE"

        if leg_name and needs_rebuild.upper() == "TRUE":
            bills = bills_by_legislator.get(leg_name, [])
            if not bills:
                print(f"Skipping {leg_name}: No processed bills available.")
                continue

            print(f"Building intelligence profile for: {leg_name}")
            prompt = f"""
            You are a strategic legislative analyst for the Michigan SBDC. 
            Synthesize the following legislative activity for legislator {leg_name}:

            {chr(10).join(bills)}

            Generate structured intelligence in JSON format with keys matching:
            "Committee_Relevance", "Biography", "Key_Issues", "Legislative_Focus", 
            "Key_Bills", "Political_Positioning", "SBDC_Framing", "Talking_Points".
            Return ONLY raw valid JSON without markdown tags.
            """
            try:
                raw_json = generate_profile_with_fallbacks(client, prompt)
                clean_json = raw_json.replace("```json", "").replace("```", "").strip()
                parsed = json.loads(clean_json)

                # Ensure row length matches headers
                while len(p_row) < len(headers):
                    p_row.append("")

                # Map generated keys to sheet columns dynamically
                for k, v in parsed.items():
                    if k in headers:
                        col_idx = headers.index(k)
                        p_row[col_idx] = str(v)

                # Set Profile_Processed = TRUE and Needs_Rebuild = FALSE
                if "Profile_Processed" in headers:
                    p_row[headers.index("Profile_Processed")] = "TRUE"
                if "Needs_Rebuild" in headers:
                    p_row[headers.index("Needs_Rebuild")] = "FALSE"

                row_num = idx + 2
                service.spreadsheets().values().update(
                    spreadsheetId=SHEET_ID,
                    range=f"Profiles_Dynamic!A{row_num}:R{row_num}",
                    valueInputOption="RAW",
                    body={"values": [p_row]}
                ).execute()

                print(f"Successfully updated profile for {leg_name}")

            except Exception as e:
                print(f"Failed to build profile for {leg_name}: {e}")

    print("Profile builder complete.")

if __name__ == "__main__":
    build_profiles()