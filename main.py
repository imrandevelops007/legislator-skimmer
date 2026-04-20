import os
import json
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build


# =========================
# Config
# =========================
SHEET_ID = os.environ["SHEET_ID"]
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# How many document links to keep per legislator
MAX_LINKS_FROM_HUB = 25

LEGISLATORS_RANGE = "Legislators!A2:F"
ACTIVITY_RANGE_ALL = "Activity_Items!A2:I"
ACTIVITY_HEADERS_RANGE = "Activity_Items!A1:I1"
PROFILES_RANGE_ALL = "Profiles_Dynamic!A2:R"
PROFILES_HEADERS_RANGE = "Profiles_Dynamic!A1:R1"


# =========================
# Google Sheets helpers
# =========================
def get_sheets_service():
    sa_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    info = json.loads(sa_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)


def sheets_get_values(service, rng: str):
    return (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SHEET_ID, range=rng)
        .execute()
        .get("values", [])
    )


def sheets_update_values(service, rng: str, rows: list[list[str]]):
    body = {"values": rows}
    service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=rng,
        valueInputOption="RAW",
        body=body
    ).execute()


def sheets_clear(service, rng: str):
    service.spreadsheets().values().clear(
        spreadsheetId=SHEET_ID,
        range=rng,
        body={}
    ).execute()


def sheets_batch_update(service, data: list[dict]):
    if not data:
        return
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={
            "valueInputOption": "RAW",
            "data": data,
        }
    ).execute()


# =========================
# Generic helpers
# =========================
def clean(value) -> str:
    return "" if value is None else str(value).strip()


def pad_row(row: list[str], length: int) -> list[str]:
    if len(row) < length:
        return row + [""] * (length - len(row))
    return row[:length]


def bool_from_cell(value) -> bool:
    return clean(value).lower() in {"true", "1", "yes", "y"}


# =========================
# URL helpers
# =========================
def same_domain(a: str, b: str) -> bool:
    return urlparse(a).netloc.lower() == urlparse(b).netloc.lower()


def normalize_url(base: str, href: str) -> str:
    return urljoin(base, href)


def canonicalize_legislature_url(u: str) -> str:
    parsed = urlparse(u)
    match = re.search(r"(objectname=[^&]+)", parsed.query, flags=re.IGNORECASE)
    if not match:
        return u
    clean_query = match.group(1)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{clean_query}"


def ensure_printer_friendly(url: str) -> str:
    if "printerfriendly=" in url.lower():
        return url
    joiner = "&" if ("?" in url) else "?"
    return f"{url}{joiner}printerFriendly=true"


# =========================
# Sheet readers
# =========================
def read_legislators(service):
    rows = sheets_get_values(service, LEGISLATORS_RANGE)
    parsed: list[tuple[str, str, str]] = []

    for r in rows:
        if not r:
            continue

        name = clean(r[0]) if len(r) >= 1 else ""
        website = clean(r[1]) if len(r) >= 2 else ""
        hub = clean(r[5]) if len(r) >= 6 else ""

        if name:
            parsed.append((name, website, hub))

    return parsed


def read_activity_rows(service) -> list[list[str]]:
    rows = sheets_get_values(service, ACTIVITY_RANGE_ALL)
    return [pad_row(r, 9) for r in rows]


def read_profiles(service) -> tuple[list[str], list[list[str]]]:
    headers = sheets_get_values(service, PROFILES_HEADERS_RANGE)
    header_row = headers[0] if headers else []
    rows = sheets_get_values(service, PROFILES_RANGE_ALL)
    return header_row, [pad_row(r, max(18, len(header_row) if header_row else 18)) for r in rows]


# =========================
# Legislature Search collector
# =========================
def collect_legislature_search_result_links(search_url: str) -> list[str]:
    search_url = ensure_printer_friendly(search_url)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        )

        page.goto(search_url, wait_until="networkidle", timeout=120000)

        for _ in range(8):
            page.evaluate("() => window.scrollBy(0, document.body.scrollHeight)")
            page.wait_for_timeout(250)

        html = page.content()

        obj_names = re.findall(
            r"objectName=([A-Za-z0-9\-]+)",
            html,
            flags=re.IGNORECASE
        )

        links: list[str] = []
        if obj_names:
            for obj in obj_names:
                u = f"https://www.legislature.mi.gov/Home/GetObject?objectName={obj}"
                links.append(canonicalize_legislature_url(u))

        if not links:
            hrefs = page.eval_on_selector_all(
                "a[href]",
                "els => els.map(e => e.getAttribute('href')).filter(Boolean)"
            )
            for href in hrefs:
                full = normalize_url(page.url, href)
                if not same_domain(page.url, full):
                    continue

                ul = full.lower()
                if "/home/getobject" in ul and "objectname=" in ul:
                    links.append(canonicalize_legislature_url(full))

        browser.close()

    seen = set()
    uniq: list[str] = []
    for u in links:
        if u in seen:
            continue
        seen.add(u)
        uniq.append(u)

    return uniq[:MAX_LINKS_FROM_HUB]


# =========================
# Profile helpers
# =========================
def ensure_profile_row_exists(service, profile_headers: list[str], profile_rows: list[list[str]], legislator_name: str) -> int | None:
    if not profile_headers:
        return None

    try:
        legislator_idx = profile_headers.index("Legislator")
        processed_idx = profile_headers.index("Profile_Processed")
        last_updated_idx = profile_headers.index("Last_Updated")
        needs_rebuild_idx = profile_headers.index("Needs_Rebuild")
    except ValueError:
        return None

    for i, row in enumerate(profile_rows):
        if clean(row[legislator_idx]) == legislator_name:
            return i

    new_row = [""] * len(profile_headers)
    new_row[legislator_idx] = legislator_name
    new_row[processed_idx] = "FALSE"
    new_row[last_updated_idx] = datetime.now(timezone.utc).isoformat()
    new_row[needs_rebuild_idx] = "TRUE"

    sheet_row_number = len(profile_rows) + 2
    sheets_update_values(service, f"Profiles_Dynamic!A{sheet_row_number}:R{sheet_row_number}", [new_row])
    profile_rows.append(new_row)
    return len(profile_rows) - 1


def mark_profiles_needs_rebuild(service, changed_legislators: list[str], profile_headers: list[str], profile_rows: list[list[str]]):
    if not changed_legislators or not profile_headers:
        return

    try:
        legislator_idx = profile_headers.index("Legislator")
        last_updated_idx = profile_headers.index("Last_Updated")
        needs_rebuild_idx = profile_headers.index("Needs_Rebuild")
    except ValueError:
        return

    updates = []
    now = datetime.now(timezone.utc).isoformat()

    for legislator_name in changed_legislators:
        row_idx = ensure_profile_row_exists(service, profile_headers, profile_rows, legislator_name)
        if row_idx is None:
            continue

        profile_rows[row_idx][last_updated_idx] = now
        profile_rows[row_idx][needs_rebuild_idx] = "TRUE"

        sheet_row = row_idx + 2
        updates.append({
            "range": f"Profiles_Dynamic!O{sheet_row}",
            "values": [[now]],
        })
        updates.append({
            "range": f"Profiles_Dynamic!R{sheet_row}",
            "values": [["TRUE"]],
        })

    sheets_batch_update(service, updates)


# =========================
# Main
# =========================
def main():
    service = get_sheets_service()
    legislators = read_legislators(service)
    activity_rows = read_activity_rows(service)
    profile_headers, profile_rows = read_profiles(service)

    now = datetime.now(timezone.utc).isoformat()

    print(f"Found {len(legislators)} legislator(s).")
    print(f"Loaded {len(activity_rows)} existing activity row(s).")

    configured_names = [name for name, _, _ in legislators]
    configured_set = set(configured_names)

    # Group existing rows by legislator and url so we can preserve enriched data
    existing_by_legislator_and_url: dict[tuple[str, str], list[str]] = {}
    existing_urls_by_legislator: dict[str, list[str]] = {}

    for row in activity_rows:
        row = pad_row(row, 9)
        url = clean(row[0])
        legislator = clean(row[1])

        if not legislator or not url:
            continue

        existing_by_legislator_and_url[(legislator, url)] = row
        existing_urls_by_legislator.setdefault(legislator, []).append(url)

    rebuilt_rows: list[list[str]] = []
    changed_legislators: list[str] = []

    for name, _home, hub_override in legislators:
        print(f"\n=== {name} ===")

        hub = clean(hub_override)
        if not hub:
            print("No hub_url provided. Skipping.")
            continue

        if "legislature.mi.gov/search/executesearch" not in hub.lower():
            print("Hub is not a legislature Search/ExecuteSearch URL. Skipping.")
            continue

        hub = ensure_printer_friendly(hub)
        print(f"Using legislature ExecuteSearch hub_url: {hub}")

        try:
            bill_links = collect_legislature_search_result_links(hub)
            print(
                f"Collected {len(bill_links)} bill link(s) from search results "
                f"(capped at {MAX_LINKS_FROM_HUB})."
            )
        except Exception as e:
            print(f"Failed to collect legislature search results: {e}")

            # preserve existing rows for this legislator if collection fails
            existing_rows_for_legislator = [
                existing_by_legislator_and_url[(name, url)]
                for url in existing_urls_by_legislator.get(name, [])
                if (name, url) in existing_by_legislator_and_url
            ]
            rebuilt_rows.extend(existing_rows_for_legislator)
            continue

        old_urls = existing_urls_by_legislator.get(name, [])
        if old_urls != bill_links:
            changed_legislators.append(name)

        kept_processed = 0
        new_unprocessed = 0

        for url in bill_links:
            key = (name, url)
            existing_row = existing_by_legislator_and_url.get(key)

            if existing_row:
                # preserve enrichment and preserve original captured timestamp
                preserved = pad_row(existing_row, 9)
                rebuilt_rows.append(preserved)

                if bool_from_cell(preserved[7]):
                    kept_processed += 1
            else:
                rebuilt_rows.append([
                    url,       # URL
                    name,      # Legislator
                    "bill",    # Type
                    now,       # Timestamp
                    "",        # Bill Number
                    "",        # Bill Title
                    "",        # Bill Summary
                    "FALSE",   # Processed
                    "",        # Notes
                ])
                new_unprocessed += 1

        removed_count = max(0, len(old_urls) - len(bill_links))
        print(
            f"Kept {len(bill_links) - new_unprocessed} existing row(s), "
            f"added {new_unprocessed} new row(s), "
            f"dropped {removed_count} old row(s). "
            f"Preserved processed={kept_processed}."
        )

    # Preserve any rows for legislators not currently in config
    other_rows = [
        row for row in activity_rows
        if clean(row[1]) not in configured_set
    ]
    if other_rows:
        print(f"\nPreserving {len(other_rows)} row(s) for non-configured legislator(s).")
        rebuilt_rows.extend(other_rows)

    # Rewrite Activity_Items body
    sheets_clear(service, ACTIVITY_RANGE_ALL)
    if rebuilt_rows:
        sheets_update_values(service, ACTIVITY_RANGE_ALL, rebuilt_rows)

    print(f"\nRebuilt Activity_Items with {len(rebuilt_rows)} total row(s).")

    if changed_legislators:
        print(f"Marking {len(changed_legislators)} legislator profile(s) for rebuild.")
        mark_profiles_needs_rebuild(service, changed_legislators, profile_headers, profile_rows)
    else:
        print("No legislator top-25 sets changed. No profile rebuild flags updated.")


if __name__ == "__main__":
    main()
