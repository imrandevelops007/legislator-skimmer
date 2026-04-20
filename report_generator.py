import os
import json
import re
from pathlib import Path
from typing import List, Dict, Tuple

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML


SHEET_ID = os.environ["SHEET_ID"]
DRIVE_REPORTS_FOLDER_ID = os.environ["DRIVE_REPORTS_FOLDER_ID"]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

ONLY_LEGISLATOR = os.getenv("ONLY_LEGISLATOR", "").strip()
OVERWRITE_EXISTING_IN_TARGET_FOLDER = (
    os.getenv("OVERWRITE_EXISTING_IN_TARGET_FOLDER", "true").strip().lower() == "true"
)

TEMPLATE_DIR = os.getenv("TEMPLATE_DIR", "").strip()
REPORT_TEMPLATE = os.getenv("REPORT_TEMPLATE", "report.html").strip() or "report.html"

LEGISLATORS_RANGE = "Legislators!A2:F"
METADATA_RANGE = "Legislator_Metadata!A2:Q"
PROFILES_RANGE = "Profiles_Dynamic!A2:R"

OUTPUT_DIR = "generated_reports"


def pad_row(row: List[str], target_len: int) -> List[str]:
    return row + [""] * (target_len - len(row))


def strip_markdown(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"__(.*?)__", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    return text.strip()


def split_loose_list(text: str) -> List[str]:
    text = strip_markdown(text)
    if not text:
        return []

    if "||" in text:
        return [strip_markdown(x) for x in text.split("||") if strip_markdown(x)]

    if "|" in text:
        return [strip_markdown(x) for x in text.split("|") if strip_markdown(x)]

    normalized = text.replace("\r", "\n")
    normalized = re.sub(r"\s*•\s*", "\n• ", normalized)
    normalized = re.sub(r"\s*-\s+", "\n- ", normalized)

    items: List[str] = []
    for raw in normalized.splitlines():
        item = raw.strip()
        if not item:
            continue
        item = re.sub(r"^[•\-–]+\s*", "", item).strip()
        if item:
            items.append(strip_markdown(item))

    return items if items else [text]


def split_key_bills(text: str) -> List[str]:
    text = strip_markdown(text)
    items = []

    if "||" in text:
        parts = [p.strip() for p in text.split("||") if p.strip()]
    else:
        parts = split_loose_list(text)

    for part in parts:
        if "::" in part:
            bill, summary = part.split("::", 1)
            items.append(f"{bill.strip()} – {summary.strip()}")
        elif ":" in part:
            bill, summary = part.split(":", 1)
            items.append(f"{bill.strip()} – {summary.strip()}")
        else:
            items.append(part.strip())

    return items


def get_party_color(party: str) -> str:
    party = (party or "").strip().lower()
    if party == "republican":
        return "#b71c1c"
    if party == "democratic":
        return "#0d47a1"
    return "#222222"


def format_party_label(party: str) -> str:
    party = (party or "").strip().lower()
    if party == "republican":
        return "Republican"
    if party == "democratic":
        return "Democratic"
    if party:
        return party.title()
    return ""


def slugify(name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    return value or "report"


def format_counties_full(counties: str) -> str:
    parts = [c.strip() for c in (counties or "").split("|") if c.strip()]
    return ", ".join(parts)


def parse_biography_items(text: str) -> List[Tuple[str, str]]:
    items = []
    for raw in split_loose_list(text):
        if ":" in raw:
            label, body = raw.split(":", 1)
            items.append((label.strip(), body.strip()))
        else:
            items.append(("", raw.strip()))
    return items


def parse_labeled_items(text: str) -> List[Tuple[str, str]]:
    items = []
    for raw in split_loose_list(text):
        if ":" in raw:
            label, body = raw.split(":", 1)
            items.append((label.strip(), body.strip()))
        else:
            items.append(("", raw.strip()))
    return items


def parse_plain_items(text: str) -> List[str]:
    return split_loose_list(text)


def parse_committee_items(text: str) -> List[Tuple[str, str]]:
    items = []
    text = strip_markdown(text)

    if "||" in text:
        raw_items = [x.strip() for x in text.split("||") if x.strip()]
    else:
        raw_items = split_loose_list(text)

    for raw in raw_items:
        if "::" in raw:
            committee, relevance = raw.split("::", 1)
            items.append((committee.strip(), relevance.strip()))
        elif ":" in raw:
            committee, relevance = raw.split(":", 1)
            items.append((committee.strip(), relevance.strip()))
        else:
            items.append((raw.strip(), ""))

    return items


def normalize_committee_name(name: str) -> str:
    value = (name or "").strip()

    if value.lower() == "appropriations":
        return "Appropriations Committee"

    value = re.sub(r"\bLEO\b", "Labor and Economic Opportunity", value)
    value = re.sub(r"\bEGLE\b", "Environment, Great Lakes, and Energy", value)

    if value.startswith("Labor and Economic Opportunity Appropriations"):
        value = value.replace(
            "Labor and Economic Opportunity Appropriations",
            "Appropriations Subcommittee on Labor and Economic Opportunity",
            1,
        )

    if value.startswith("LEO Appropriations"):
        value = value.replace(
            "LEO Appropriations",
            "Appropriations Subcommittee on Labor and Economic Opportunity",
            1,
        )

    if value.startswith("School Aid"):
        value = value.replace(
            "School Aid",
            "Appropriations Subcommittee on School Aid and Department of Education",
            1,
        )

    if value.startswith("Higher Education"):
        value = value.replace(
            "Higher Education",
            "Appropriations Subcommittee on Higher Education and Community Colleges",
            1,
        )

    return value


def parse_year_from_date(date_text: str) -> int | None:
    match = re.search(r"(\d{4})", date_text or "")
    if not match:
        return None
    return int(match.group(1))


def extract_previous_service_ranges(row: Dict[str, str]) -> List[Tuple[int, int]]:
    texts = [
        row.get("Time_In_Office_Note", "") or "",
        row.get("First_Elected_to_Current_Chamber", "") or "",
        row.get("Time_In_Office_Summary", "") or "",
    ]

    ranges: List[Tuple[int, int]] = []

    for text in texts:
        lowered = text.lower()

        patterns = [
            r"previously served.*?(\d{4})\s*(?:-|–|to)\s*(\d{4})",
            r"prior service.*?(\d{4})\s*(?:-|–|to)\s*(\d{4})",
            r"prior house service.*?(\d{4})\s*(?:-|–|to)\s*(\d{4})",
            r"returned after.*?(\d{4})\s*(?:-|–|to)\s*(\d{4})",
            r"non-consecutive.*?(\d{4})\s*(?:-|–|to)\s*(\d{4})",
        ]

        for pattern in patterns:
            for start, end in re.findall(pattern, lowered):
                start_year = int(start)
                end_year = int(end)
                if end_year >= start_year:
                    ranges.append((start_year, end_year))

        # fallback: capture plain year ranges only if the text suggests prior service
        if any(keyword in lowered for keyword in ["prior", "previous", "returned", "non-consecutive"]):
            for start, end in re.findall(r"(\d{4})\s*(?:-|–|to)\s*(\d{4})", lowered):
                start_year = int(start)
                end_year = int(end)
                if end_year >= start_year:
                    ranges.append((start_year, end_year))

    deduped = []
    seen = set()
    for item in ranges:
        if item not in seen:
            deduped.append(item)
            seen.add(item)

    return deduped

def estimate_legislative_service_years(row: Dict[str, str]) -> int | None:
    note = row.get("Time_In_Office_Note", "") or ""
    first_elected_text = row.get("First_Elected_to_Current_Chamber", "") or ""
    current_term_start = row.get("Current_Term_Start", "") or ""
    current_term_end = row.get("Current_Term_End", "") or ""

    start_year = parse_year_from_date(current_term_start)
    end_year = parse_year_from_date(current_term_end)

    if not start_year or not end_year or end_year <= start_year:
        return None

    current_term_years = end_year - start_year

    since_match = re.search(
        r"since\s+(?:jan\.?\s*1,\s*)?(\d{4})",
        note,
        flags=re.IGNORECASE
    )
    if since_match and "prior" not in note.lower() and "previous" not in note.lower():
        since_year = int(since_match.group(1))
        if end_year > since_year:
            return end_year - since_year

    # also allow a simple continuous-service interpretation from first elected field
    first_elected_year = parse_year_from_date(first_elected_text)
    if first_elected_year and not any(
        keyword in first_elected_text.lower()
        for keyword in ["prior", "previous", "returned", "non-consecutive"]
    ):
        if end_year > first_elected_year:
            return end_year - first_elected_year

    total_years = current_term_years

    for prior_start, prior_end in extract_previous_service_ranges(row):
        total_years += (prior_end - prior_start + 1)

    return total_years


def build_term_limit_note(row: Dict[str, str]) -> str:
    projected_years = estimate_legislative_service_years(row)
    if projected_years is None:
        return ""

    remaining = 12 - projected_years

    if remaining <= 0:
        return (
            f"Projected to reach Michigan's 12-year legislative service cap at the end of this term "
            f"({projected_years} years total); another legislative run would likely not be available."
        )

    if remaining >= 4:
        return (
            f"Projected legislative service at the end of this term: {projected_years} years; "
            f"still below Michigan's 12-year cap and eligible to seek another legislative term."
        )

    if remaining >= 2:
        return (
            f"Projected legislative service at the end of this term: {projected_years} years; "
            f"still below Michigan's 12-year cap, though future eligibility will depend on the next term sought."
        )

    return (
        f"Projected legislative service at the end of this term: {projected_years} years; "
        f"near Michigan's 12-year cap, so future eligibility would likely be limited."
    )


def get_creds():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    return Credentials.from_service_account_info(info, scopes=SCOPES)


def get_sheets_service():
    return build("sheets", "v4", credentials=get_creds())


def get_drive_service():
    return build("drive", "v3", credentials=get_creds())


def sheets_get_values(service, rng: str) -> List[List[str]]:
    return (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SHEET_ID, range=rng)
        .execute()
        .get("values", [])
    )


def load_legislators(service) -> Dict[str, Dict[str, str]]:
    rows = sheets_get_values(service, LEGISLATORS_RANGE)
    out = {}

    for row in rows:
        row = pad_row(row, 6)
        legislator = row[0].strip()
        if not legislator:
            continue

        out[legislator] = {
            "Legislator": row[0].strip(),
            "Website_URL": row[1].strip(),
            "Region": row[2].strip(),
            "Tier": row[3].strip(),
            "Last_Checked": row[4].strip(),
            "Hub_URL": row[5].strip(),
        }

    return out


def load_metadata(service) -> Dict[str, Dict[str, str]]:
    rows = sheets_get_values(service, METADATA_RANGE)
    out = {}

    for row in rows:
        row = pad_row(row, 17)
        legislator = row[0].strip()
        if not legislator:
            continue

        out[legislator] = {
            "Legislator": row[0].strip(),
            "Chamber": row[1].strip(),
            "District": row[2].strip(),
            "Party": row[3].strip(),
            "First_Elected_to_Current_Chamber": row[4].strip(),
            "Current_Term_Start": row[5].strip(),
            "Current_Term_End": row[6].strip(),
            "Time_In_Office_Note": row[7].strip(),
            "Education": row[8].strip(),
            "Professional_Background": row[9].strip(),
            "Government_Experience": row[10].strip(),
            "Committee_Assignments": row[11].strip(),
            "Key_Issues_Source": row[12].strip(),
            "Political_Positioning_Source": row[13].strip(),
            "Verification_Notes": row[14].strip(),
            "Image_URL": row[15].strip(),
            "Counties": row[16].strip(),
        }

    return out


def load_profiles(service) -> Dict[str, Dict[str, str]]:
    rows = sheets_get_values(service, PROFILES_RANGE)
    out = {}

    for row in rows:
        row = pad_row(row, 18)
        legislator = row[0].strip()
        if not legislator:
            continue

        if row[15].strip().upper() != "TRUE":
            continue

        out[legislator] = {
            "Legislator": row[0].strip(),
            "Committee_Relevance_Summary": row[1].strip(),
            "Time_In_Office_Summary": row[2].strip(),
            "Generated_Biography": row[3].strip(),
            "Key_Issues": row[4].strip(),
            "District_Development_Signals": row[5].strip(),
            "Legislative_Focus_Areas": row[6].strip(),
            "Key_Bills": row[7].strip(),
            "Political_Positioning": row[8].strip(),
            "Political_Positioning_Bullets": row[9].strip(),
            "SBDC_Framing": row[10].strip(),
            "Talking_Points": row[11].strip(),
            "Bills_Analyzed_Count": row[12].strip(),
            "Source_Bill_Numbers": row[13].strip(),
            "Last_Updated": row[14].strip(),
            "Profile_Processed": row[15].strip(),
            "Notes": row[16].strip(),
            "Needs_Rebuild": row[17].strip() if len(row) > 17 else "",
        }

    return out


def resolve_template_path() -> tuple[str, str]:
    """
    Returns (template_dir, template_name)
    """
    candidates = []

    if TEMPLATE_DIR:
        candidates.append(Path(TEMPLATE_DIR) / REPORT_TEMPLATE)

    candidates.extend([
        Path(REPORT_TEMPLATE),
        Path("templates") / REPORT_TEMPLATE,
        Path(__file__).resolve().parent / REPORT_TEMPLATE,
        Path(__file__).resolve().parent / "templates" / REPORT_TEMPLATE,
    ])

    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            print(f"Using template file: {candidate}")
            return str(candidate.parent), candidate.name

    checked = "\n".join(str(c) for c in candidates)
    raise FileNotFoundError(
        f"Could not find template '{REPORT_TEMPLATE}'. Checked these paths:\n{checked}"
    )


def list_existing_files_in_target_folder(drive_service, folder_id: str, filename: str):
    escaped_name = filename.replace("'", r"\\'")
    query = (
        f"'{folder_id}' in parents and "
        f"name = '{escaped_name}' and "
        f"trashed = false"
    )

    response = (
        drive_service.files()
        .list(
            q=query,
            fields="files(id, name, webViewLink)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            corpora="allDrives",
        )
        .execute()
    )

    return response.get("files", [])


def upload_pdf_to_drive(drive_service, local_path: str, filename: str, folder_id: str) -> str:
    existing_files = list_existing_files_in_target_folder(drive_service, folder_id, filename)

    if OVERWRITE_EXISTING_IN_TARGET_FOLDER and existing_files:
        existing = existing_files[0]
        file_id = existing["id"]
        print(f"Updating existing Drive file in target folder: {existing['name']} ({file_id})")

        try:
            media = MediaFileUpload(local_path, mimetype="application/pdf", resumable=True)

            updated = (
                drive_service.files()
                .update(
                    fileId=file_id,
                    media_body=media,
                    fields="id, name, webViewLink",
                    supportsAllDrives=True,
                )
                .execute()
            )
            print(f"Updated in Drive: {updated['name']} ({updated['id']})")
            return updated.get("webViewLink", "")
        except HttpError as e:
            status = getattr(e.resp, "status", None)

            if status != 404:
                raise

            print(f"Existing file could not be updated because it was not found anymore: {file_id}")
            print("Falling back to fresh upload...")

    file_metadata = {
        "name": filename,
        "parents": [folder_id],
        "mimeType": "application/pdf",
    }

    media = MediaFileUpload(local_path, mimetype="application/pdf", resumable=True)

    created = (
        drive_service.files()
        .create(
            body=file_metadata,
            media_body=media,
            fields="id, name, webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )

    print(f"Uploaded to Drive: {created['name']} ({created['id']})")
    return created.get("webViewLink", "")


def render_html(row: Dict[str, str], template_dir: str, template_name: str) -> str:
    env = Environment(loader=FileSystemLoader(template_dir))
    template = env.get_template(template_name)

    chamber_label = row["Chamber"]
    if chamber_label.lower() == "senate":
        chamber_label = "Senate"
    elif chamber_label.lower() == "house":
        chamber_label = "House"

    party_label = format_party_label(row["Party"])
    region = row.get("Region", "").strip()
    counties = format_counties_full(row.get("Counties", ""))

    if region and counties:
        location_line = f"{region} | {counties}"
    elif region:
        location_line = region
    else:
        location_line = counties

    time_in_office_items = parse_plain_items(row["Time_In_Office_Summary"])
    term_limit_note = build_term_limit_note(row)
    if term_limit_note:
        time_in_office_items.append(term_limit_note)

    committee_items = [
        (normalize_committee_name(name), note)
        for name, note in parse_committee_items(row["Committee_Relevance_Summary"])
    ]

    return template.render(
        name=row["Legislator"],
        chamber=chamber_label,
        district=row["District"],
        party_label=party_label,
        party_color=get_party_color(row["Party"]),
        location_line=location_line,
        image_url=row["Image_URL"],
        committee_items=committee_items,
        time_in_office=time_in_office_items,
        bio=parse_biography_items(row["Generated_Biography"]),
        issues=parse_labeled_items(row["Key_Issues"]),
        district_signals=parse_plain_items(row["District_Development_Signals"]),
        focus=parse_plain_items(row["Legislative_Focus_Areas"]),
        bills=split_key_bills(row["Key_Bills"]),
        positioning=strip_markdown(row["Political_Positioning"]),
        positioning_notes=parse_plain_items(row["Political_Positioning_Bullets"]),
        sbdc=strip_markdown(row["SBDC_Framing"]),
        talking=parse_plain_items(row["Talking_Points"]),
    )


def write_pdf(html_string: str, output_path: str) -> None:
    HTML(string=html_string).write_pdf(output_path)


def main():
    sheets_service = get_sheets_service()
    drive_service = get_drive_service()

    legislators_by_name = load_legislators(sheets_service)
    metadata_by_name = load_metadata(sheets_service)
    profiles_by_name = load_profiles(sheets_service)

    print(f"Loaded legislators config rows: {len(legislators_by_name)}")
    print(f"Loaded metadata rows: {len(metadata_by_name)}")
    print(f"Loaded processed profile rows: {len(profiles_by_name)}")

    template_dir, template_name = resolve_template_path()

    legislators = sorted(
        set(legislators_by_name.keys()) &
        set(metadata_by_name.keys()) &
        set(profiles_by_name.keys())
    )

    if ONLY_LEGISLATOR:
        print(f"Filtering to ONLY_LEGISLATOR: {ONLY_LEGISLATOR}")
        legislators = [x for x in legislators if x == ONLY_LEGISLATOR]

    if not legislators:
        if ONLY_LEGISLATOR and ONLY_LEGISLATOR not in profiles_by_name:
            print(
                f"No report generated because '{ONLY_LEGISLATOR}' does not currently have "
                f"a processed profile row with Profile_Processed=TRUE in Profiles_Dynamic."
            )
        else:
            print("No legislators matched across Legislators, Legislator_Metadata, and processed Profiles_Dynamic rows.")

    print(f"Generating reports for {len(legislators)} legislator(s)...")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    generated_count = 0
    uploaded_count = 0
    skipped_count = 0

    for legislator in legislators:
        merged = {}
        merged.update(legislators_by_name[legislator])
        merged.update(metadata_by_name[legislator])
        merged.update(profiles_by_name[legislator])

        if not merged.get("Image_URL"):
            print(f"Skipping {legislator}: missing Image_URL in Legislator_Metadata.")
            skipped_count += 1
            continue

        html = render_html(merged, template_dir, template_name)

        slug = slugify(legislator)
        filename = f"{slug}.pdf"
        output_path = os.path.join(OUTPUT_DIR, filename)

        write_pdf(html, output_path)
        print(f"Generated report for {legislator}: {output_path}")
        generated_count += 1

        try:
            drive_link = upload_pdf_to_drive(
                drive_service=drive_service,
                local_path=output_path,
                filename=filename,
                folder_id=DRIVE_REPORTS_FOLDER_ID,
            )
            uploaded_count += 1
            if drive_link:
                print(f"Drive link: {drive_link}")
        except Exception as e:
            print(f"Drive upload failed for {legislator}: {e}")

    print(f"Done. Generated={generated_count}, Uploaded={uploaded_count}, Skipped={skipped_count}")


if __name__ == "__main__":
    main()
