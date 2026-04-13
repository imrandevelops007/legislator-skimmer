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
MARK_PROFILES_PROCESSED = os.getenv("MARK_PROFILES_PROCESSED", "false").strip().lower() == "true"

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


def split_lines(value: str) -> List[str]:
    if not value:
        return []
    return [line.strip("• ").strip() for line in str(value).splitlines() if line.strip()]


def bullets_to_html(items: List[str]) -> str:
    if not items:
        return ""
    lis = "".join(f"<li>{html.escape(item)}</li>" for item in items)
    return f"<ul>{lis}</ul>"


def rich_text_to_html(value: str) -> str:
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


def read_sheet_with_row_numbers(service, spreadsheet_id: str, tab_name: str) -> List[Dict[str, Any]]:
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=tab_name
    ).execute()

    values = result.get("values", [])
    if not values:
        return []

    headers = [normalize_header(h) for h in values[0]]
    rows = []

    for i, row in enumerate(values[1:], start=2):
        padded = row + [""] * (len(headers) - len(row))
        item = {headers[j]: padded[j] for j in range(len(headers))}
        item["_sheet_row_number"] = i
        rows.append(item)

    return rows


def update_cell(service, spreadsheet_id: str, tab_name: str, a1_range: str, value: str) -> None:
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{tab_name}!{a1_range}",
        valueInputOption="RAW",
        body={"values": [[value]]},
    ).execute()


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


def get_raw_headers(service, spreadsheet_id: str, tab_name: str) -> List[str]:
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{tab_name}!1:1"
    ).execute()

    values = result.get("values", [])
    return values[0] if values else []


def find_profile_processed_column_letter(profile_headers: List[str]) -> Optional[str]:
    normalized = [normalize_header(h) for h in profile_headers]

    for candidate in ["profile_processed", "processed"]:
        if candidate in normalized:
            idx = normalized.index(candidate)
            break
    else:
        return None

    n = idx + 1
    letters = ""
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


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
    legislator_name = clean_cell(
        profile_row.get("legislator")
        or metadata_row.get("legislator")
        or legislator_row.get("legislator")
        or legislator_row.get("name")
    )

    party = clean_cell(metadata_row.get("party"))
    district = clean_cell(metadata_row.get("district"))
    chamber = clean_cell(metadata_row.get("chamber"))
    region = clean_cell(legislator_row.get("region"))
    counties = clean_cell(metadata_row.get("counties"))
    image_url = clean_cell(metadata_row.get("image_url"))

    political_positioning_main = clean_cell(profile_row.get("political_positioning"))
    political_positioning_bullets = clean_cell(profile_row.get("political_positioning_bullets"))

    political_positioning_combined = "\n".join(
        part for part in [political_positioning_main, political_positioning_bullets] if part
    )

    bills_analyzed_count = clean_cell(profile_row.get("bills_analyzed_count"))
    source_bill_numbers = clean_cell(profile_row.get("source_bill_numbers"))
    last_updated = clean_cell(profile_row.get("last_updated"))
    notes = clean_cell(profile_row.get("notes"))

    context = {
        "legislator": legislator_name,
        "party": party,
        "district": district,
        "chamber": chamber,
        "region": region,
        "counties": counties,
        "image_url": image_url,
        "party_color": extract_party_color(party),

        "committee_relevance_html": rich_text_to_html(profile_row.get("committee_relevance_summary", "")),
        "time_in_office_html": rich_text_to_html(profile_row.get("time_in_office_summary", "")),
        "biography_html": rich_text_to_html(profile_row.get("generated_biography", "")),
        "key_issues_html": rich_text_to_html(profile_row.get("key_issues", "")),
        "district_signals_html": rich_text_to_html(profile_row.get("district_development_signals", "")),
        "legislative_focus_html": rich_text_to_html(profile_row.get("legislative_focus_areas", "")),
        "key_bills_html": rich_text_to_html(profile_row.get("key_bills", "")),
        "political_positioning_html": rich_text_to_html(political_positioning_combined),
        "sbdc_framing_html": rich_text_to_html(profile_row.get("sbdc_framing", "")),
        "talking_points_html": rich_text_to_html(profile_row.get("talking_points", "")),

        "committee_relevance_raw": clean_cell(profile_row.get("committee_relevance_summary")),
        "time_in_office_raw": clean_cell(profile_row.get("time_in_office_summary")),
        "biography_raw": clean_cell(profile_row.get("generated_biography")),
        "key_issues_raw": clean_cell(profile_row.get("key_issues")),
        "district_signals_raw": clean_cell(profile_row.get("district_development_signals")),
        "legislative_focus_raw": clean_cell(profile_row.get("legislative_focus_areas")),
        "key_bills_raw": clean_cell(profile_row.get("key_bills")),
        "political_positioning_raw": political_positioning_combined,
        "sbdc_framing_raw": clean_cell(profile_row.get("sbdc_framing")),
        "talking_points_raw": clean_cell(profile_row.get("talking_points")),

        "bills_analyzed_count": bills_analyzed_count,
        "source_bill_numbers": source_bill_numbers,
        "last_updated": last_updated,
        "notes": notes,
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
# Main Generation Flow
# =========================

def main():
    ensure_dir(OUTPUT_DIR)

    print("Building Google API clients...")
    sheets_service = get_sheets_service()
    drive_service = get_drive_service()

    print("Reading sheet data...")
    legislators_rows = read_sheet_as_dicts(sheets_service, SHEET_ID, TAB_LEGISLATORS)
    metadata_rows = read_sheet_as_dicts(sheets_service, SHEET_ID, TAB_METADATA)
    profile_rows = read_sheet_with_row_numbers(sheets_service, SHEET_ID, TAB_PROFILES)

    if not legislators_rows:
        raise RuntimeError(f"No rows found in tab '{TAB_LEGISLATORS}'")
    if not metadata_rows:
        raise RuntimeError(f"No rows found in tab '{TAB_METADATA}'")
    if not profile_rows:
        raise RuntimeError(f"No rows found in tab '{TAB_PROFILES}'")

    legislators_index = build_index(legislators_rows, ["legislator", "name"])
    metadata_index = build_index(metadata_rows, ["legislator", "name"])

    raw_profile_headers = get_raw_headers(sheets_service, SHEET_ID, TAB_PROFILES)
    processed_col_letter = find_profile_processed_column_letter(raw_profile_headers)

    env = load_template_env(TEMPLATE_DIR)

    generated_count = 0
    uploaded_count = 0

    for profile_row in profile_rows:
        legislator_name = clean_cell(profile_row.get("legislator") or profile_row.get("name"))
        if not legislator_name:
            print("Skipping profile row with no legislator name.")
            continue

        if ONLY_LEGISLATOR and legislator_name.lower() != ONLY_LEGISLATOR.lower():
            continue

        profile_processed = (
            profile_row.get("profile_processed")
            or profile_row.get("processed")
            or ""
        )

        if ONLY_PROCESSED_PROFILES and not bool_from_cell(profile_processed):
            print(f"Skipping unprocessed profile: {legislator_name}")
            continue

        legislator_row = legislators_index.get(legislator_name.lower(), {})
        metadata_row = metadata_index.get(legislator_name.lower(), {})

        if not metadata_row:
            print(f"Warning: No metadata row found for {legislator_name}. Continuing with partial data.")

        context = prepare_report_context(legislator_row, metadata_row, profile_row)

        safe_name = slugify_filename(legislator_name)
        filename = f"{safe_name}_briefing.pdf"
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

            if MARK_PROFILES_PROCESSED and processed_col_letter:
                row_number = profile_row["_sheet_row_number"]
                cell_ref = f"{processed_col_letter}{row_number}"
                update_cell(sheets_service, SHEET_ID, TAB_PROFILES, cell_ref, "TRUE")

            print(f"Drive link: {uploaded.get('webViewLink', 'No link returned')}")

        except HttpError as e:
            print(f"Drive upload failed for {legislator_name}: {e}")

    print("Done.")
    print(f"Generated PDFs: {generated_count}")
    print(f"Uploaded PDFs:  {uploaded_count}")


if __name__ == "__main__":
    main()
