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

# How many document links to collect per legislator per run
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


def ensure_printer_friendly(url: str) -> str:
    if "printerfriendly=" in url.lower():
        return url
    joiner = "&" if ("?" in url) else "?"
    return f"{url}{joiner}printerFriendly=true"


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
    parsed: list[tuple[str, str, str]] = []
    for r in rows:
        if not r:
            continue
        name = (r[0] or "").strip() if len(r) >= 1 else ""
        website = (r[1] or "").strip() if len(r) >= 2 else ""
        hub = (r[5] or "").strip() if len(r) >= 6 else ""
        if name:
            parsed.append((name, website, hub))
    return parsed


def read_seen_pairs(service) -> set[tuple[str, str]]:
    """
    Reads SeenURLs!A2:B and returns a set of (url, legislator_name).
    This makes "seen" tracking per-legislator instead of global-by-URL.
    """
    rows = sheets_get_values(service, "SeenURLs!A2:B")
    out: set[tuple[str, str]] = set()
    for r in rows:
        if not r or len(r) < 2:
            continue
        url = (r[0] or "").strip()
        name = (r[1] or "").strip()
        if url and name:
            out.add((url, name))
    return out


# =========================
# Legislature Search collector (Playwright)
# =========================
def collect_legislature_search_result_links(search_url: str) -> list[str]:
    """
    Extract bill/resolution detail links from legislature Search/ExecuteSearch pages.
    Returns canonical GetObject URLs with queryID stripped.
    """
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

        # Light scroll to encourage any deferred rendering
        for _ in range(8):
            page.evaluate("() => window.scrollBy(0, document.body.scrollHeight)")
            page.wait_for_timeout(250)

        html = page.content()

        # Stage A: regex objectName extraction (works even if links aren't normal anchors)
        obj_names = re.findall(r"objectName=([A-Za-z0-9\-]+)", html, flags=re.IGNORECASE)

        links: list[str] = []
        if obj_names:
            for obj in obj_names:
                u = f"https://www.legislature.mi.gov/Home/GetObject?objectName={obj}"
                links.append(canonicalize_legislature_url(u))

        # Stage B: fallback to anchor href extraction if regex yields nothing
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

    # de-dupe preserving order
    seen = set()
    uniq: list[str] = []
    for u in links:
        if u in seen:
            continue
        seen.add(u)
        uniq.append(u)

    return uniq[:MAX_LINKS_FROM_HUB]


# =========================
# Main (Bill-only mode)
# =========================
def main():
    service = get_sheets_service()
    legislators = read_legislators(service)

    # Per-legislator seen tracking
    seen_pairs = read_seen_pairs(service)

    now = datetime.now(timezone.utc).isoformat()
    print(f"Found {len(legislators)} legislator(s). Already seen {len(seen_pairs)} (url,name) pair(s).")

    new_seen_rows: list[list[str]] = []
    new_activity_rows: list[list[str]] = []

    for name, _home, hub_override in legislators:
        print(f"\n=== {name} ===")

        hub = (hub_override or "").strip()
        if not hub:
            print("No hub_url provided. Skipping (bill-only mode).")
            continue

        if "legislature.mi.gov/search/executesearch" not in hub.lower():
            print("Hub is not a legislature Search/ExecuteSearch URL. Skipping (bill-only mode).")
            continue

        hub = ensure_printer_friendly(hub)
        print(f"Using legislature ExecuteSearch hub_url: {hub}")

        try:
            bill_links = collect_legislature_search_result_links(hub)
            print(f"Collected {len(bill_links)} bill link(s) from search results (capped at {MAX_LINKS_FROM_HUB}).")
        except Exception as e:
            print(f"Failed to collect legislature search results: {e}")
            continue

        added = 0
        for u in bill_links:
            # Safety: enforce legislature GetObject only
            ul = u.lower()
            if "/home/getobject" not in ul or "objectname=" not in ul:
                continue

            key = (u, name)
            if key in seen_pairs:
                continue

            # SeenURLs per legislator
            new_seen_rows.append([u, name, now])
            seen_pairs.add(key)

            # Activity_Items per legislator
            new_activity_rows.append([u, name, "bill", now, "", "", ""])
            added += 1

        print(f"Added {added} new bill(s).")

    sheets_append_values(service, SEENURLS_RANGE_APPEND, new_seen_rows)
    sheets_append_values(service, ACTIVITY_RANGE_APPEND, new_activity_rows)

    print(f"\nAppended {len(new_seen_rows)} new row(s) to SeenURLs.")
    print(f"Appended {len(new_activity_rows)} new row(s) to Activity_Items.")


if __name__ == "__main__":
    main()
