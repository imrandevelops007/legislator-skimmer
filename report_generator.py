import os
import re
import json
import html
from pathlib import Path
from typing import Dict, List, Any, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape
from weasyprint import HTML, CSS

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from googleapiclient.errors import HttpError


# =========================
# Config
# =========================

SHEET_ID = os.environ["SHEET_ID"]
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
DRIVE_REPORTS_FOLDER_ID = os.environ["DRIVE_REPORTS_FOLDER_ID"]

OVERWRITE_EXISTING_IN_TARGET_FOLDER = (
    os.getenv("OVERWRITE_EXISTING_IN_TARGET_FOLDER", "true").strip().lower() == "true"
)

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "generated_reports"))
TEMPLATE_DIR = Path(os.getenv("TEMPLATE_DIR", "templates"))
TEMPLATE_NAME = os.getenv("REPORT_TEMPLATE", "report.html")
STATIC_DIR = Path(os.getenv("STATIC_DIR", str(TEMPLATE_DIR)))

TAB_LEGISLATORS = os.getenv("TAB_LEGISLATORS", "Legislators")
TAB_METADATA = os.getenv("TAB_METADATA", "Legislator_Metadata")
TAB_PROFILES = os.getenv("TAB_PROFILES", "Profiles_Dynamic")

ONLY_PROCESSED_PROFILES = os.getenv("ONLY_PROCESSED_PROFILES", "true").strip().lower() == "true"
ONLY_LEGISLATOR = os.getenv("ONLY_LEGISLATOR", "").strip()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


# =========================
# Helpers
# =========================

def normalize_header(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")


def slugify_filename(text: str) -> str:
    text = text.strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[-\s]+", "_", text)
    return text.strip("_")


def bool_from_cell(value: Any) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in {"true", "yes", "1", "y"}


def clean_cell(value: Any) -> str:
    return "" if value is None else str(value).strip()


def split_lines(value: Any) -> List[str]:
    text = clean_cell(value)
    if not text:
        return []
    return [line.strip("• ").strip() for line in text.splitlines() if line.strip()]


def multiline_text(value: Any) -> str:
    return "\n".join(split_lines(value))


def bullets_to_html(items: List[str]) -> str:
    if not items:
        return ""
    lis = "".join(f"<li>{html.escape(item)}</li>" for item in items)
    return f"<ul>{lis}</ul>"


def rich_text_to_html(value: Any) -> str:
    return bullets_to_html(split_lines(value))


def extract_party_color(party: str) -> str:
    party = clean_cell(party).lower()
    if party.startswith("d"):
        return "#1D4ED8"
    if party.startswith("r"):
        return "#B91C1C"
    return "#374151"


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def require_profile_columns(profile_rows: List[Dict[str, Any]]) -> None:
    required = {
        "legislator",
        "committee_relevance_summary",
        "time_in_office_summary",
        "generated_biography",
        "key_issues",
        "district_development_signals",
        "legislative_focus_areas",
        "key_bills",
        "political_positioning",
        "political_positioning_bullets",
        "sbdc_framing",
        "talking_points",
        "bills_analyzed_count",
        "source_bill_numbers",
        "last_updated",
        "profile_processed",
        "notes",
    }

    if not profile_rows:
        raise RuntimeError(f"No rows found in tab '{TAB_PROFILES}'")

    available = set(profile_rows[0].keys())
    missing = sorted(col for col in required if col not in available)
    if missing:
        raise RuntimeError(
            f"Profiles_Dynamic is missing required column(s): {', '.join(missing)}"
        )


# =========================
# Google API Clients
# =========================

def get_credentials() -> Credentials:
    service_account_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    return Credentials.from_service_account_info(service_account_info, scopes=SCOPES)


def get_sheets_service():
    creds = get_credentials()
    return build("sheets", "v4", credentials=creds)


def get_drive_service():
    creds = get_credentials()
    return build("drive", "v3", credentials=creds)


# =========================
# Google Sheets Access
# =========================

def read_sheet_as_dicts(service, spreadsheet_id: str, tab_name: str) -> List[Dict[str, str]]:
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=tab_name
    ).execute()

    values = result.get("values", [])
    if not values:
        return []

    headers = [normalize_header(h) for h in values[0]]
    rows = []

    for row in values[1:]:
        padded = row + [""] * (len(headers) - len(row))
        rows.append({headers[i]: padded[i] for i in range(len(headers))})

    return rows


def build_index(rows: List[Dict[str, Any]], key_candidates: List[str]) -> Dict[str, Dict[str, Any]]:
    index = {}
    for row in rows:
        key = ""
        for candidate in key_candidates:
            if clean_cell(row.get(candidate)):
                key = clean_cell(row.get(candidate))
                break
        if key:
            index[key.lower()] = row
    return index


# =========================
# Drive Upload Logic
# =========================

def list_files_with_same_name_in_folder(drive_service, folder_id: str, filename: str) -> List[Dict[str, str]]:
    escaped_name = filename.replace("'", r"\'")
    query = (
        f"'{folder_id}' in parents and "
        f"name = '{escaped_name}' and "
        f"trashed = false"
    )

    response = drive_service.files().list(
        q=query,
        fields="files(id, name, webViewLink)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        corpora="allDrives",
    ).execute()

    return response.get("files", [])


def delete_drive_file(drive_service, file_id: str) -> None:
    drive_service.files().delete(
        fileId=file_id,
        supportsAllDrives=True,
    ).execute()


def upload_pdf_to_drive(
    drive_service,
    pdf_path: Path,
    folder_id: str,
    overwrite_existing: bool = True,
) -> Dict[str, str]:
    filename = pdf_path.name

    if overwrite_existing:
        existing_files = list_files_with_same_name_in_folder(drive_service, folder_id, filename)
        for existing in existing_files:
            print(f"Deleting existing file in target folder only: {existing['name']} ({existing['id']})")
            delete_drive_file(drive_service, existing["id"])

    file_metadata = {
        "name": filename,
        "parents": [folder_id],
        "mimeType": "application/pdf",
    }

    with pdf_path.open("rb") as f:
        media = MediaIoBaseUpload(f, mimetype="application/pdf", resumable=True)

        created = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id, name, webViewLink",
            supportsAllDrives=True,
        ).execute()

    print(f"Uploaded to Drive: {created['name']} ({created['id']})")
    return created


# =========================
# Report Rendering
# =========================

def load_template_env(template_dir: Path) -> Environment:
    return Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "xml"]),
    )


def prepare_report_context(
    legislator_row: Dict[str, Any],
    metadata_row: Dict[str, Any],
    profile_row: Dict[str, Any],
) -> Dict[str, Any]:
    legislator_name = clean_cell(profile_row["legislator"])
    party = clean_cell(metadata_row.get("party"))
    district = clean_cell(metadata_row.get("district"))
    chamber = clean_cell(metadata_row.get("chamber"))
    region = clean_cell(legislator_row.get("region"))
    counties = clean_cell(metadata_row.get("counties"))
    image_url = clean_cell(metadata_row.get("image_url"))

    political_positioning_combined = "\n".join(
        part for part in [
            multiline_text(profile_row["political_positioning"]),
            multiline_text(profile_row["political_positioning_bullets"]),
        ] if part
    )

    context = {
        # Identity / header
        "legislator": legislator_name,
        "party": party,
        "district": district,
        "chamber": chamber,
        "region": region,
        "counties": counties,
        "image_url": image_url,
        "party_color": extract_party_color(party),

        # Plain text fields for template
        "committee_relevance": multiline_text(profile_row["committee_relevance_summary"]),
        "time_in_office": multiline_text(profile_row["time_in_office_summary"]),
        "biography": multiline_text(profile_row["generated_biography"]),
        "key_issues": multiline_text(profile_row["key_issues"]),
        "district_signals": multiline_text(profile_row["district_development_signals"]),
        "legislative_focus": multiline_text(profile_row["legislative_focus_areas"]),
        "key_bills": multiline_text(profile_row["key_bills"]),
        "political_positioning": political_positioning_combined,
        "sbdc_framing": multiline_text(profile_row["sbdc_framing"]),
        "talking_points": multiline_text(profile_row["talking_points"]),

        # HTML fields if template uses them
        "committee_relevance_html": rich_text_to_html(profile_row["committee_relevance_summary"]),
        "time_in_office_html": rich_text_to_html(profile_row["time_in_office_summary"]),
        "biography_html": rich_text_to_html(profile_row["generated_biography"]),
        "key_issues_html": rich_text_to_html(profile_row["key_issues"]),
        "district_signals_html": rich_text_to_html(profile_row["district_development_signals"]),
        "legislative_focus_html": rich_text_to_html(profile_row["legislative_focus_areas"]),
        "key_bills_html": rich_text_to_html(profile_row["key_bills"]),
        "political_positioning_html": rich_text_to_html(
            "\n".join(
                part for part in [
                    clean_cell(profile_row["political_positioning"]),
                    clean_cell(profile_row["political_positioning_bullets"]),
                ] if part
            )
        ),
        "sbdc_framing_html": rich_text_to_html(profile_row["sbdc_framing"]),
        "talking_points_html": rich_text_to_html(profile_row["talking_points"]),

        # Extra metadata
        "bills_analyzed_count": clean_cell(profile_row["bills_analyzed_count"]),
        "source_bill_numbers": clean_cell(profile_row["source_bill_numbers"]),
        "last_updated": clean_cell(profile_row["last_updated"]),
        "notes": clean_cell(profile_row["notes"]),
    }

    return context


def render_pdf(
    env: Environment,
    template_name: str,
    context: Dict[str, Any],
    output_path: Path,
) -> None:
    template = env.get_template(template_name)
    html_content = template.render(**context)

    css_path = STATIC_DIR / "report.css"
    stylesheets = [CSS(filename=str(css_path))] if css_path.exists() else []

    HTML(string=html_content, base_url=str(TEMPLATE_DIR.resolve())).write_pdf(
        str(output_path),
        stylesheets=stylesheets,
    )


# =========================
# Main
# =========================

def main():
    ensure_dir(OUTPUT_DIR)

    print("Building Google API clients...")
    sheets_service = get_sheets_service()
    drive_service = get_drive_service()

    print("Reading sheet data...")
    legislators_rows = read_sheet_as_dicts(sheets_service, SHEET_ID, TAB_LEGISLATORS)
    metadata_rows = read_sheet_as_dicts(sheets_service, SHEET_ID, TAB_METADATA)
    profile_rows = read_sheet_as_dicts(sheets_service, SHEET_ID, TAB_PROFILES)

    if not legislators_rows:
        raise RuntimeError(f"No rows found in tab '{TAB_LEGISLATORS}'")
    if not metadata_rows:
        raise RuntimeError(f"No rows found in tab '{TAB_METADATA}'")
    if not profile_rows:
        raise RuntimeError(f"No rows found in tab '{TAB_PROFILES}'")

    require_profile_columns(profile_rows)

    legislators_index = build_index(legislators_rows, ["legislator", "name"])
    metadata_index = build_index(metadata_rows, ["legislator", "name"])
    env = load_template_env(TEMPLATE_DIR)

    generated_count = 0
    uploaded_count = 0

    for profile_row in profile_rows:
        legislator_name = clean_cell(profile_row["legislator"])
        if not legislator_name:
            print("Skipping profile row with no legislator name.")
            continue

        if ONLY_LEGISLATOR and legislator_name.lower() != ONLY_LEGISLATOR.lower():
            continue

        if ONLY_PROCESSED_PROFILES and not bool_from_cell(profile_row["profile_processed"]):
            print(f"Skipping unprocessed profile: {legislator_name}")
            continue

        legislator_row = legislators_index.get(legislator_name.lower(), {})
        metadata_row = metadata_index.get(legislator_name.lower(), {})

        if not metadata_row:
            print(f"Warning: No metadata row found for {legislator_name}. Continuing with partial data.")

        context = prepare_report_context(legislator_row, metadata_row, profile_row)

        filename = f"{slugify_filename(legislator_name)}.pdf"
        output_path = OUTPUT_DIR / filename

        print(f"Generating PDF for {legislator_name} -> {output_path}")
        render_pdf(env, TEMPLATE_NAME, context, output_path)
        generated_count += 1

        try:
            uploaded = upload_pdf_to_drive(
                drive_service=drive_service,
                pdf_path=output_path,
                folder_id=DRIVE_REPORTS_FOLDER_ID,
                overwrite_existing=OVERWRITE_EXISTING_IN_TARGET_FOLDER,
            )
            uploaded_count += 1
            print(f"Drive link: {uploaded.get('webViewLink', 'No link returned')}")
        except HttpError as e:
            print(f"Drive upload failed for {legislator_name}: {e}")

    print("Done.")
    print(f"Generated PDFs: {generated_count}")
    print(f"Uploaded PDFs:  {uploaded_count}")


if __name__ == "__main__":
    main()
