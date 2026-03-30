import os
import json
import re
from typing import List, Dict

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML


# =========================
# Config
# =========================
SHEET_ID = os.environ["SHEET_ID"]
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

ONLY_LEGISLATOR = os.getenv("ONLY_LEGISLATOR", "").strip()

LEGISLATORS_RANGE = "Legislators!A2:F"
METADATA_RANGE = "Legislator_Metadata!A2:Q"
PROFILES_RANGE = "Profiles_Dynamic!A2:Q"

OUTPUT_DIR = "generated_reports"


# Legislators columns:
# A  Legislator
# B  Website_URL
# C  Region
# D  Tier
# E  Last_Checked
# F  Hub_URL

# Legislator_Metadata columns:
# A  Legislator
# B  Chamber
# C  District
# D  Party
# E  First_Elected_to_Current_Chamber
# F  Current_Term_Start
# G  Current_Term_End
# H  Time_In_Office_Note
# I  Education
# J  Professional_Background
# K  Government_Experience
# L  Committee_Assignments
# M  Key_Issues_Source
# N  Political_Positioning_Source
# O  Verification_Notes
# P  Image_URL
# Q  Counties

# Profiles_Dynamic columns:
# A  Legislator
# B  Committee_Relevance_Summary
# C  Time_In_Office_Summary
# D  Generated_Biography
# E  Key_Issues
# F  District_Development_Signals
# G  Legislative_Focus_Areas
# H  Key_Bills
# I  Political_Positioning
# J  Political_Positioning_Bullets
# K  SBDC_Framing
# L  Talking_Points
# M  Bills_Analyzed_Count
# N  Source_Bill_Numbers
# O  Last_Updated
# P  Profile_Processed
# Q  Notes


# =========================
# Helpers
# =========================
def pad_row(row: List[str], target_len: int) -> List[str]:
    return row + [""] * (target_len - len(row))


def split_pipe(text: str) -> List[str]:
    return [x.strip() for x in (text or "").split("|") if x.strip()]


def split_key_bills(text: str) -> List[str]:
    items = []
    for part in (text or "").split("||"):
        part = part.strip()
        if not part:
            continue
        if "::" in part:
            bill, summary = part.split("::", 1)
            items.append(f"{bill.strip()} – {summary.strip()}")
        else:
            items.append(part)
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


def format_counties(counties: str, max_display: int = 3) -> str:
    parts = [c.strip() for c in (counties or "").split("|") if c.strip()]

    if not parts:
        return ""

    if len(parts) <= max_display:
        return ", ".join(parts)

    shown = parts[:max_display]
    remaining = len(parts) - max_display
    return f"{', '.join(shown)} + {remaining} more"


# =========================
# Google API clients
# =========================
def get_creds():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    return Credentials.from_service_account_info(info, scopes=SCOPES)


def get_sheets_service():
    return build("sheets", "v4", credentials=get_creds())


def sheets_get_values(service, rng: str) -> List[List[str]]:
    return (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SHEET_ID, range=rng)
        .execute()
        .get("values", [])
    )


# =========================
# Load data
# =========================
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
        row = pad_row(row, 17)
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
        }

    return out


# =========================
# Rendering
# =========================
def render_html(row: Dict[str, str]) -> str:
    env = Environment(loader=FileSystemLoader("templates"))
    template = env.get_template("report.html")

    chamber_label = row["Chamber"]
    if chamber_label.lower() == "senate":
        chamber_label = "Senate"
    elif chamber_label.lower() == "house":
        chamber_label = "House"

    party_label = format_party_label(row["Party"])
    region = row.get("Region", "").strip()
    counties = format_counties(row.get("Counties", ""))

    location_line = ""
    if region and counties:
        location_line = f"{region} • {counties}"
    elif region:
        location_line = region
    elif counties:
        location_line = counties

    return template.render(
        name=row["Legislator"],
        chamber=chamber_label,
        district=row["District"],
        party=row["Party"],
        party_label=party_label,
        party_color=get_party_color(row["Party"]),
        location_line=location_line,
        image_url=row["Image_URL"],
        committee=row["Committee_Relevance_Summary"],
        time_in_office=split_pipe(row["Time_In_Office_Summary"]),
        bio=split_pipe(row["Generated_Biography"]),
        issues=split_pipe(row["Key_Issues"]),
        district_signals=split_pipe(row["District_Development_Signals"]),
        focus=split_pipe(row["Legislative_Focus_Areas"]),
        bills=split_key_bills(row["Key_Bills"]),
        positioning=row["Political_Positioning"],
        positioning_notes=split_pipe(row["Political_Positioning_Bullets"]),
        sbdc=row["SBDC_Framing"],
        talking=split_pipe(row["Talking_Points"]),
    )


def write_pdf(html_string: str, output_path: str) -> None:
    HTML(string=html_string).write_pdf(output_path)


# =========================
# Main
# =========================
def main():
    sheets_service = get_sheets_service()

    legislators_by_name = load_legislators(sheets_service)
    metadata_by_name = load_metadata(sheets_service)
    profiles_by_name = load_profiles(sheets_service)

    legislators = sorted(
        set(legislators_by_name.keys()) &
        set(metadata_by_name.keys()) &
        set(profiles_by_name.keys())
    )

    if ONLY_LEGISLATOR:
        legislators = [x for x in legislators if x == ONLY_LEGISLATOR]

    print(f"Generating reports for {len(legislators)} legislator(s)...")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    generated_count = 0
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

        html = render_html(merged)

        slug = slugify(legislator)
        filename = f"{slug}.pdf"
        output_path = os.path.join(OUTPUT_DIR, filename)

        write_pdf(html, output_path)
        print(f"Generated report for {legislator}: {output_path}")
        generated_count += 1

    print(f"Done. Generated={generated_count}, Skipped={skipped_count}")


if __name__ == "__main__":
    main()
