import os
import re
import jinja2
from weasyprint import HTML
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

# Configuration loaded from environment variables
SHEET_ID = os.getenv("SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
DRIVE_REPORTS_FOLDER_ID = os.getenv("DRIVE_REPORTS_FOLDER_ID")

# Updated template directory fallback to include 'templates' folder
TEMPLATE_DIR = os.getenv("TEMPLATE_DIR", "templates")
REPORT_TEMPLATE = os.getenv("REPORT_TEMPLATE", "report.html")
OVERWRITE_EXISTING_IN_TARGET_FOLDER = (
    os.getenv("OVERWRITE_EXISTING_IN_TARGET_FOLDER", "true").lower() == "true"
)
ONLY_LEGISLATOR = os.getenv("ONLY_LEGISLATOR", "").strip()

# Default placeholder image URL used when Image_URL is missing in Legislator_Metadata
DEFAULT_PLACEHOLDER_IMAGE = "https://via.placeholder.com/150?text=No+Photo"


def sanitize_filename(name):
    """Sanitize legislator name for safe file path creation."""
    return re.sub(r"[^\w\-_]", "_", name)


def init_google_drive_service():
    """Initialize Google Drive API client using service account credentials."""
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON environment variable is missing.")
    
    scopes = ["https://www.googleapis.com/auth/drive"]
    
    # Handle JSON string or file path
    if os.path.exists(GOOGLE_SERVICE_ACCOUNT_JSON):
        creds = Credentials.from_service_account_file(
            GOOGLE_SERVICE_ACCOUNT_JSON, scopes=scopes
        )
    else:
        import json
        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = Credentials.from_service_account_info(info, scopes=scopes)

    return build("drive", "v3", credentials=creds)


def upload_or_update_drive_file(drive_service, file_path, file_name, folder_id):
    """Upload a PDF report to Google Drive or update it if it already exists."""
    query = f"'{folder_id}' in parents and name = '{file_name}' and trashed = false"
    response = drive_service.files().list(q=query, fields="files(id, name)").execute()
    files = response.get("files", [])

    from googleapiclient.http import MediaFileUpload

    media = MediaFileUpload(file_path, mimetype="application/pdf", resumable=True)

    if files and OVERWRITE_EXISTING_IN_TARGET_FOLDER:
        file_id = files[0]["id"]
        print(f"Updating existing Drive file in target folder: {file_name} ({file_id})")
        updated_file = (
            drive_service.files()
            .update(fileId=file_id, media_body=media, fields="id, webViewLink")
            .execute()
        )
        print(f"Updated in Drive: {file_name} ({updated_file.get('id')})")
        print(f"Drive link: {updated_file.get('webViewLink')}")
        return updated_file.get("webViewLink")
    else:
        file_metadata = {"name": file_name, "parents": [folder_id]}
        created_file = (
            drive_service.files()
            .create(body=file_metadata, media_body=media, fields="id, webViewLink")
            .execute()
        )
        print(f"Uploaded to Drive: {file_name} ({created_file.get('id')})")
        print(f"Drive link: {created_file.get('webViewLink')}")
        return created_file.get("webViewLink")


def main():
    # Search both '.' and 'templates' directories for report.html
    search_paths = [TEMPLATE_DIR, ".", "templates"]
    template_loader = jinja2.FileSystemLoader(searchpath=search_paths)
    jinja_env = jinja2.Environment(loader=template_loader)
    
    template = jinja_env.get_template(REPORT_TEMPLATE)

    # Initialize Google Drive Client if configured
    drive_service = None
    if DRIVE_REPORTS_FOLDER_ID:
        try:
            drive_service = init_google_drive_service()
        except Exception as e:
            print(f"Warning: Google Drive client initialization failed: {e}")

    output_dir = "generated_reports"
    os.makedirs(output_dir, exist_ok=True)

    # In your original code, profiles and metadata are loaded from Google Sheets here.
    # Below shows where the missing Image_URL fallback check occurs:
    #
    # for profile in processed_profiles:
    #     legislator_name = profile.get("Legislator", "").strip()
    #     metadata = metadata_dict.get(legislator_name, {})
    #
    #     image_url = metadata.get("Image_URL")
    #     if not image_url or str(image_url).strip().lower() in ["nan", "none", ""]:
    #         image_url = DEFAULT_PLACEHOLDER_IMAGE
    #     metadata["Image_URL"] = image_url

    print("Report generator initialized successfully.")


if __name__ == "__main__":
    main()