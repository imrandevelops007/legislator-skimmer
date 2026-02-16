import os
import json
from datetime import datetime
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SHEET_ID = os.environ.get("SHEET_ID")  # you will set this in GitHub secrets
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

def get_sheets_service():
    sa_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    info = json.loads(sa_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)

def read_legislators(service):
    result = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range="Legislators!A2:E"
    ).execute()
    return result.get("values", [])

def main():
    service = get_sheets_service()
    rows = read_legislators(service)
    print("Legislators rows:", rows)
    print("Ran at:", datetime.utcnow().isoformat(), "UTC")

if __name__ == "__main__":
    main()
