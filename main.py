import os
import json
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
from playwright.sync_api import sync_playwright

import requests
from bs4 import BeautifulSoup

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build


SHEET_ID = os.environ["SHEET_ID"]
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Universal-ish keywords to find a "hub" page on a legislator site
HUB_KEYWORDS = [
    "press", "press-releases", "press releases",
    "news", "media", "updates", "blog", "articles", "releases"
]

# When on a hub page, we will collect links that look like posts
# (this stays generic: we'll just grab a bunch and filter by domain + uniqueness)
MAX_LINKS_FROM_HUB = 25


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


def fetch_html(url: str) -> str:
    # 1) Try normal HTTP request first (fast path)
    try:
        r = requests.get(
            url,
            timeout=25,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

        # If blocked (403), trigger fallback
        if r.status_code == 403:
            raise requests.HTTPError("403 Forbidden", response=r)

        r.raise_for_status()
        return r.text

    except Exception as e:
        print(f"requests fetch failed for {url}: {e} | trying Playwright fallback...")

        # 2) Universal fallback using Playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)

            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                )
            )

            # Optional performance boost: block heavy assets
            def block_heavy(route):
                if route.request.resource_type in ("image", "media", "font"):
                    route.abort()
                else:
                    route.continue_()

            page.route("**/*", block_heavy)

            # Use DOMContentLoaded instead of networkidle
            page.goto(url, wait_until="domcontentloaded", timeout=120000)

            # Let dynamic content settle
            page.wait_for_timeout(2000)

            html = page.content()

            browser.close()

            return html


def normalize_url(base: str, href: str) -> str:
    return urljoin(base, href)


def same_domain(a: str, b: str) -> bool:
    return urlparse(a).netloc.lower() == urlparse(b).netloc.lower()


def find_hub_url(home_url: str) -> str | None:
    """
    Universal discovery:
    - fetch homepage
    - scan <a href> for likely hub pages (press/news/blog)
    - return the best-looking candidate
    """
    html = fetch_html(home_url)
    soup = BeautifulSoup(html, "lxml")

    candidates: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        text = (a.get_text(" ", strip=True) or "").lower()
        href_l = href.lower()

        if not href:
            continue

        # Basic keyword match on link text OR URL path
        if any(k in text for k in HUB_KEYWORDS) or any(k in href_l for k in HUB_KEYWORDS):
            full = normalize_url(home_url, href)
            # keep only same-domain links
            if same_domain(home_url, full):
                candidates.append(full)

    # Prefer links that literally contain "press-releases" or "press"
    preferred = [c for c in candidates if "press" in c.lower()]
    if preferred:
        # choose the shortest (often the hub page, not a specific article)
        return sorted(set(preferred), key=len)[0]

    if candidates:
        return sorted(set(candidates), key=len)[0]

    return None


def collect_links_from_hub(hub_url: str) -> list[str]:
    """
    Generic collection:
    - fetch hub page
    - collect a bunch of same-domain links
    - de-duplicate
    - remove obvious non-content links
    """
    html = fetch_html(hub_url)
    soup = BeautifulSoup(html, "lxml")

    links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href:
            continue
        full = normalize_url(hub_url, href)
        if not same_domain(hub_url, full):
            continue
        links.append(full)

    # de-dupe while preserving order
    seen = set()
    uniq = []
    for u in links:
        if u in seen:
            continue
        seen.add(u)
        uniq.append(u)

    # filter out junk
    junk_patterns = [
        r"#", r"mailto:", r"tel:",
        r"/wp-admin", r"/wp-login", r"/privacy", r"/terms",
        r"/contact", r"/donate", r"/subscribe", r"/search"
    ]

    def is_junk(u: str) -> bool:
        ul = u.lower()
        return any(re.search(p, ul) for p in junk_patterns)

    cleaned = [u for u in uniq if not is_junk(u)]

    return cleaned[:MAX_LINKS_FROM_HUB]


def read_legislators(service):
    # Legislators!A2:F = name, website_url, district, tier, last_checked, hub_url
    rows = sheets_get_values(service, "Legislators!A2:F")
    parsed = []
    for r in rows:
        if len(r) < 2:
            continue
        name = r[0].strip()
        website = r[1].strip()
        hub = r[5].strip() if len(r) >= 6 and r[5].strip() else ""
        if name and website:
            parsed.append((name, website, hub))
    return parsed


def read_seenurls(service) -> set[str]:
    rows = sheets_get_values(service, "SeenURLs!A2:A")
    return set(r[0].strip() for r in rows if r and r[0].strip())


def main():
    service = get_sheets_service()

    legislators = read_legislators(service)
    seen_urls = read_seenurls(service)

    now = datetime.now(timezone.utc).isoformat()

    print(f"Found {len(legislators)} legislator(s). Already seen {len(seen_urls)} URL(s).")

    new_rows = []

    for name, home, hub_override in legislators:
        print(f"\n=== {name} ===")
        print(f"Home: {home}")

        hub = None

        if hub_override:
            hub = hub_override
            print(f"Using hub_url from sheet: {hub}")
        else:
            try:
                hub = find_hub_url(home)
            except Exception as e:
                print(f"Failed to fetch/parse homepage: {e}")
                continue
        
            if not hub:
                print("No hub page found (press/news/blog).")
                continue
        
            print(f"Hub found: {hub}")

        try:
            links = collect_links_from_hub(hub)
        except Exception as e:
            print(f"Failed to fetch/parse hub page: {e}")
            continue

        # Only keep links that look like actual content pages:
        # heuristic: exclude the hub itself and keep longer URLs
        content_links = [u for u in links if u != hub and len(urlparse(u).path.strip("/")) > 2]

        # Add unseen links
        added = 0
        for u in content_links:
            if u in seen_urls:
                continue
            new_rows.append([u, name, now])
            seen_urls.add(u)
            added += 1

        print(f"Collected {len(content_links)} candidate links; added {added} new.")

    # Write new rows to SeenURLs
    sheets_append_values(service, "SeenURLs!A:C", new_rows)
    print(f"\nAppended {len(new_rows)} new URL(s) to SeenURLs.")


if __name__ == "__main__":
    main()
