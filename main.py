import os
import json
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from playwright.sync_api import sync_playwright

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build


# =========================
# Config
# =========================
SHEET_ID = os.environ["SHEET_ID"]
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

MAX_LINKS_FROM_HUB = 25

# Legislators columns expected:
# A: name
# B: website (not used in bill-only mode, but kept for compatibility)
# F: hub_url override (MUST be a legislature Search/ExecuteSearch URL)
LEGISLATORS_RANGE = "Legislators!A2:F"

# SeenURLs columns:
# A url
# B legislator_name
# C captured_at
SEENURLS_RANGE_APPEND = "SeenURLs!A:C"

# Activity_Items columns:
# A url
# B legislator_name
# C source_type (always "bill" in bill-only mode)
# D captured_at
# E title (blank for now)
# F summary (blank for now)
# G issue_tags (blank for now)
ACTIVITY_RANGE_APPEND = "Activity_Items!A:G"


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


def sheets_append_values(service, rng: str, rows: list[list[str]]):
    if not rows:
        return
    body = {"values": rows}
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=rng,
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body=body
    ).execute()


# =========================
# URL helpers
# =========================
def same_domain(a: str, b: str) -> bool:
    return urlparse(a).netloc.lower() == urlparse(b).netloc.lower()


def normalize_url(base: str, href: str) -> str:
    return urljoin(base, href)


def canonicalize_legislature_url(u: str) -> str:
    """
    Remove tracking params like queryID from GetObject URLs.
    Keeps only objectName.
    Example:
      ...GetObject?objectName=2025-HB-4102&queryID=123 -> ...GetObject?objectName=2025-HB-4102
    """
    parsed = urlparse(u)
    m = re.search(r"(objectname=[^&]+)", parsed.query, flags=re.IGNORECASE)
    if not m:
        return u
    clean_query = m.group(1)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{clean_query}"


# =========================
# Sheet readers
# =========================
def read_legislators(service):
    """
    Reads Legislators!A2:F
    A=name, B=website_url, F=hub_url override (required in bill-only mode)
    Returns list of tuples: (name, website_url, hub_url_or_blank)
    """
    rows = sheets_get_values(service, LEGISLATORS_RANGE)
    parsed = []
    for r in rows:
        if len(r) < 2:
            continue
        name = (r[0] or "").strip()
        website = (r[1] or "").strip()
        hub = (r[5] or "").strip() if len(r) >= 6 else ""
        if name:
            parsed.append((name, website, hub))
    return parsed


def read_seenurls(service) -> set[str]:
    rows = sheets_get_values(service, "SeenURLs!A2:A")
    return set((r[0] or "").strip() for r in rows if r and (r[0] or "").strip())


# =========================
# Legislature Search collector (Playwright)
# =========================
def collect_legislature_search_result_links(search_url: str) -> list[str]:
    """
    Extract bill/resolution detail links from legislature Search/ExecuteSearch pages.
    Returns canonical GetObject URLs with queryID stripped.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(search_url, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(2500)

        hrefs = page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => e.getAttribute('href')).filter(Boolean)"
        )
        browser.close()

    full_links = []
    for href in hrefs:
        full = normalize_url(search_url, href)
        if same_domain(search_url, full):
            full_links.append(full)

    # de-dupe preserving order
    seen = set()
    uniq = []
    for u in full_links:
        if u in seen:
            continue
        seen.add(u)
        uniq.append(u)

    bill_links = []
    for u in uniq:
        ul = u.lower()
        if "/home/getobject" in ul and "objectname=" in ul:
            bill_links.append(canonicalize_legislature_url(u))

    return bill_links[:MAX_LINKS_FROM_HUB]


# =========================
# Main (Bill-only mode)
# =========================
def main():
    service = get_sheets_service()
    legislators = read_legislators(service)
    seen_urls = read_seenurls(service)

    now = datetime.now(timezone.utc).isoformat()
    print(f"Found {len(legislators)} legislator(s). Already seen {len(seen_urls)} URL(s).")

    new_seen_rows: list[list[str]] = []
    new_activity_rows: list[list[str]] = []

    for name, home, hub_override in legislators:
        print(f"\n=== {name} ===")

        if not hub_override:
            print("No hub_url provided. Skipping (bill-only mode).")
            continue

        hub = hub_override.strip()
        print(f"Using hub_url from sheet: {hub}")

        if "legislature.mi.gov/search/executesearch" not in hub.lower():
            print("Hub is not a legislature Search/ExecuteSearch URL. Skipping (bill-only mode).")
            continue

        try:
            bill_links = collect_legislature_search_result_links(hub)
            print(f"Collected {len(bill_links)} bill link(s).")
        except Exception as e:
            print(f"Failed to collect legislature search results: {e}")
            continue

        added = 0
        for u in bill_links:
            if u in seen_urls:
                continue

            # SeenURLs
            new_seen_rows.append([u, name, now])
            seen_urls.add(u)

            # Activity_Items (title/summary/tags filled later by analyze script)
            new_activity_rows.append([u, name, "bill", now, "", "", ""])
            added += 1

        print(f"Added {added} new bill(s).")

    sheets_append_values(service, SEENURLS_RANGE_APPEND, new_seen_rows)
    sheets_append_values(service, ACTIVITY_RANGE_APPEND, new_activity_rows)

    print(f"\nAppended {len(new_seen_rows)} new URL(s) to SeenURLs.")
    print(f"Appended {len(new_activity_rows)} new row(s) to Activity_Items.")


if __name__ == "__main__":
    main()
