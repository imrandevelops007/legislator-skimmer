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
TEMPLATE_DIR = os.getenv("TEMPLATE_DIR", ".")
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
    # Setup Jinja2 Template Environment
    template_loader = jinja2.FileSystemLoader(searchpath=TEMPLATE_DIR)
    jinja_env = jinja2.Environment(loader=template_loader)
    template = jinja_env.get_template(REPORT_TEMPLATE)

    # Initialize Google Drive Client if configured
    drive_service = None
    if DRIVE_REPORTS_FOLDER_ID:
        try:
            drive_service = init_google_drive_service()
        except Exception as e:
            print(f"Warning: Google Drive client initialization failed: {e}")

    # Note: Replace this section with your Google Sheets/CSV loader logic as structured in your environment
    # Example placeholder structure assuming pandas/gspread reads:
    # legislators_df = load_sheet("Legislators")
    # metadata_df = load_sheet("Legislator_Metadata")
    # profiles_df = load_sheet("Profiles_Dynamic")

    # In your script's execution loop:
    output_dir = "generated_reports"
    os.makedirs(output_dir, exist_ok=True)

    # Load profile records from the sheet
    # Assuming `processed_profiles` is populated from `Profiles_Dynamic`:
    # processed_profiles = profiles_df.to_dict(orient="records")

    print("Generating PDF reports...")
    
    # OPTION 2 UPDATED LOGIC:
    # Instead of skipping when Image_URL is missing, we check and apply DEFAULT_PLACEHOLDER_IMAGE
    generated_count = 0
    uploaded_count = 0
    skipped_count = 0

    # Iterate over dynamic profiles or combined legislators data
    # (Adapted to your script's main rendering loop):
    for profile in processed_profiles:
        legislator_name = profile.get("Legislator", "").strip()
        
        if not legislator_name or legislator_name.lower() == "nan":
            continue

        if ONLY_LEGISLATOR and legislator_name.lower() != ONLY_LEGISLATOR.lower():
            continue

        # Look up corresponding metadata
        # metadata = metadata_dict.get(legislator_name, {})

        # --- OPTION 2 FIX START ---
        # Read Image_URL, fallback to default placeholder if missing or NaN
        image_url = metadata.get("Image_URL")
        if not image_url or str(image_url).strip().lower() in ["nan", "none", ""]:
            image_url = DEFAULT_PLACEHOLDER_IMAGE
            print(f"Notice: {legislator_name} is missing Image_URL in Legislator_Metadata. Using placeholder image.")
        
        # Inject the validated or fallback image_url back into metadata context
        metadata["Image_URL"] = image_url
        # --- OPTION 2 FIX END ---

        # Render HTML report
        context = {
            "legislator": profile,
            "metadata": metadata,
            "image_url": image_url
        }
        
        rendered_html = template.render(context)

        # Output PDF path
        pdf_filename = f"{sanitize_filename(legislator_name)}.pdf"
        pdf_path = os.path.join(output_dir, pdf_filename)

        # Generate PDF using WeasyPrint
        HTML(string=rendered_html).write_pdf(pdf_path)
        print(f"Generated report for {legislator_name}: {pdf_path}")
        generated_count += 1

        # Upload to Google Drive if configured
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