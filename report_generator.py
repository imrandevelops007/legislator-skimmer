import json
import os
import re
import jinja2
from weasyprint import HTML
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from google.oauth2.service_account import Credentials

# Configuration loaded from environment variables
SHEET_ID = os.getenv("SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
DRIVE_REPORTS_FOLDER_ID = os.getenv("DRIVE_REPORTS_FOLDER_ID")
TEMPLATE_DIR = os.getenv("TEMPLATE_DIR", ".")
REPORT_TEMPLATE = os.getenv("REPORT_TEMPLATE", "report.html")
OVERWRITE_EXISTING_IN_TARGET_FOLDER = (
    os.getenv("OVERWRITE_EXISTING_IN_TARGET_FOLDER", "true").lower() == "true"
)
ONLY_LEGISLATOR = os.getenv("ONLY_LEGISLATOR", "").strip()

# Default fallback image URL when Image_URL is missing
DEFAULT_PLACEHOLDER_IMAGE = "https://via.placeholder.com/150?text=No+Photo"


def sanitize_filename(name):
    """Sanitize legislator name for safe file path creation."""
    return re.sub(r"[^\w\-_]", "_", name)


def init_google_services():
    """Initialize Sheets and Drive API clients using service account credentials."""
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON environment variable is missing.")
    
    scopes = [
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/spreadsheets.readonly"
    ]
    
    if os.path.exists(GOOGLE_SERVICE_ACCOUNT_JSON):
        creds = Credentials.from_service_account_file(
            GOOGLE_SERVICE_ACCOUNT_JSON, scopes=scopes
        )
    else:
        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = Credentials.from_service_account_info(info, scopes=scopes)

    drive_service = build("drive", "v3", credentials=creds)
    sheets_service = build("sheets", "v4", credentials=creds)
    return drive_service, sheets_service


def fetch_sheet_records(sheets_service, sheet_id, range_name):
    """Fetch rows from Google Sheet tab and convert to list of dictionaries."""
    result = (
        sheets_service.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=range_name)
        .execute()
    )
    rows = result.get("values", [])
    if not rows:
        return []
    
    headers = [str(h).strip() for h in rows[0]]
    records = []
    for row in rows[1:]:
        record = {}
        for idx, header in enumerate(headers):
            record[header] = row[idx] if idx < len(row) else ""
        records.append(record)
    return records


def upload_or_update_drive_file(drive_service, file_path, file_name, folder_id):
    """Upload a PDF report to Google Drive or overwrite it in the existing folder."""
    query = f"'{folder_id}' in parents and name = '{file_name}' and trashed = false"
    response = drive_service.files().list(q=query, fields="files(id, name)").execute()
    files = response.get("files", [])

    media = MediaFileUpload(file_path, mimetype="application/pdf", resumable=True)

    # Attempt to update if file exists
    if files and OVERWRITE_EXISTING_IN_TARGET_FOLDER:
        file_id = files[0]["id"]
        try:
            print(f"Updating existing Drive file: {file_name} ({file_id})")
            updated_file = (
                drive_service.files()
                .update(fileId=file_id, media_body=media, fields="id, webViewLink")
                .execute()
            )
            print(f"Updated in Drive: {file_name}")
            return updated_file.get("webViewLink")
        except HttpError as e:
            if e.resp.status in [403, 404]:
                print(f"File ID {file_id} not writable/found ({e.resp.status}). Uploading fresh file into target folder...")
            else:
                raise e

    # Fallback: Create new file directly in target folder
    file_metadata = {"name": file_name, "parents": [folder_id]}
    created_file = (
        drive_service.files()
        .create(body=file_metadata, media_body=media, fields="id, webViewLink")
        .execute()
    )
    print(f"Uploaded to Drive: {file_name} ({created_file.get('id')})")
    return created_file.get("webViewLink")


def main():
    drive_service, sheets_service = init_google_services()

    # Load records from Google Sheets
    legislators = fetch_sheet_records(sheets_service, SHEET_ID, "Legislators!A1:Z")
    metadata_list = fetch_sheet_records(sheets_service, SHEET_ID, "Legislator_Metadata!A1:Z")
    profiles = fetch_sheet_records(sheets_service, SHEET_ID, "Profiles_Dynamic!A1:Z")

    print(f"Loaded legislators config rows: {len(legislators)}")
    print(f"Loaded metadata rows: {len(metadata_list)}")

    processed_profiles = [
        p for p in profiles 
        if p.get("Legislator") and str(p.get("Legislator")).strip() != "" and str(p.get("Legislator")).lower() != "nan"
    ]
    print(f"Loaded processed profile rows: {len(processed_profiles)}")

    search_paths = [TEMPLATE_DIR, ".", "templates"]
    template_loader = jinja2.FileSystemLoader(searchpath=search_paths)
    jinja_env = jinja2.Environment(loader=template_loader)
    template = jinja_env.get_template(REPORT_TEMPLATE)
    print(f"Using template file: {REPORT_TEMPLATE}")

    metadata_dict = {
        str(m.get("Legislator", "")).strip(): m for m in metadata_list if m.get("Legislator")
    }

    output_dir = "generated_reports"
    os.makedirs(output_dir, exist_ok=True)

    generated_count = 0
    uploaded_count = 0
    skipped_count = 0

    print(f"Generating reports for {len(processed_profiles)} legislator(s)...")

    for profile in processed_profiles:
        legislator_name = str(profile.get("Legislator", "")).strip()

        if ONLY_LEGISLATOR and legislator_name.lower() != ONLY_LEGISLATOR.lower():
            continue

        metadata = metadata_dict.get(legislator_name, {})

        image_url = metadata.get("Image_URL")
        if not image_url or str(image_url).strip().lower() in ["nan", "none", ""]:
            image_url = DEFAULT_PLACEHOLDER_IMAGE

        metadata["Image_URL"] = image_url

        context = {
            "legislator": profile,
            "metadata": metadata,
            "image_url": image_url,
            **profile,
            **metadata
        }
        rendered_html = template.render(context)

        pdf_filename = f"{sanitize_filename(legislator_name)}.pdf"
        pdf_path = os.path.join(output_dir, pdf_filename)

        HTML(string=rendered_html).write_pdf(pdf_path)
        print(f"Generated report for {legislator_name}: {pdf_path}")
        generated_count += 1

        if drive_service and DRIVE_REPORTS_FOLDER_ID:
            try:
                upload_or_update_drive_file(
                    drive_service, pdf_path, pdf_filename, DRIVE_REPORTS_FOLDER_ID
                )
                uploaded_count += 1
            except Exception as e:
                print(f"Error uploading {pdf_filename} to Drive: {e}")

    print(f"Done. Generated={generated_count}, Uploaded={uploaded_count}, Skipped={skipped_count}")


if __name__ == "__main__":
    main()