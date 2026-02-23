import os
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build


# =========================
# Config
# =========================
SHEET_ID = os.environ["SHEET_ID"]
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

HUB_KEYWORDS = [
    "press", "press-releases", "press releases",
    "news", "media", "updates", "blog", "articles", "releases"
]

MAX_LINKS_FROM_HUB = 25

# Activity_Items columns:
# A url
# B legislator_name
# C source_type
# D captured_at
# E title (blank for now)
# F summary (blank for now)
# G issue_tags (blank for now)
ACTIVITY_RANGE_APPEND = "Activity_Items!A:G"
SEENURLS_RANGE_APPEND = "SeenURLs!A:C"


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
# URL + fetch helpers
# =========================
def normalize_url(base: str, href: str) -> str:
    return urljoin(base, href)


def same_domain(a: str, b: str) -> bool:
    return urlparse(a).netloc.lower() == urlparse(b).netloc.lower()


def is_legislature_url(u: str) -> bool:
    return "legislature.mi.gov" in urlparse(u).netloc.lower()


def canonicalize_legislature_url(u: str) -> str:
    """
    Remove tracking params like queryID from GetObject URLs.
    Keeps only objectName.
    """
    parsed = urlparse(u)
    q = parsed.query
    m = re.search(r"(objectname=[^&]+)", q, flags=re.IGNORECASE)
    if not m:
        return u
    clean_query = m.group(1)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{clean_query}"


def fetch_html(url: str) -> str:
    """
    Try requests first; if blocked or fails, fall back to Playwright.
    """
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
        if r.status_code == 403:
            raise requests.HTTPError("403 Forbidden", response=r)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"requests fetch failed for {url}: {e} | trying Playwright fallback...")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                )
            )

            # Avoid heavy resources
            def block_heavy(route):
                if route.request.resource_type in ("image", "media", "font"):
                    route.abort()
                else:
                    route.continue_()

            page.route("**/*", block_heavy)

            page.goto(url, wait_until="domcontentloaded", timeout=120000)
            page.wait_for_timeout(2000)

            html = page.content()
            browser.close()
            return html


# =========================
# Sheet readers
# =========================
def read_legislators(service):
    """
    Reads Legislators tab.
    Expected columns (A-F):
      A name
      B website_url
      ...
      F hub_url (optional override)

    Returns list of tuples: (name, website_url, hub_url_or_blank)
    """
    rows = sheets_get_values(service, "Legislators!A2:F")
    parsed = []
    for r in rows:
        if len(r) < 2:
            continue
        name = (r[0] or "").strip()
        website = (r[1] or "").strip()
        hub = (r[5] or "").strip() if len(r) >= 6 else ""
        if name and website:
            parsed.append((name, website, hub))
    return parsed


def read_seenurls(service) -> set[str]:
    rows = sheets_get_values(service, "SeenURLs!A2:A")
    return set((r[0] or "").strip() for r in rows if r and (r[0] or "").strip())


# =========================
# Hub discovery + link heuristics
# =========================
def find_hub_url(home_url: str) -> str | None:
    html = fetch_html(home_url)
    soup = BeautifulSoup(html, "lxml")

    candidates: list[str] = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        text = (a.get_text(" ", strip=True) or "").lower()
        href_l = href.lower()
        if not href:
            continue

        if any(k in text for k in HUB_KEYWORDS) or any(k in href_l for k in HUB_KEYWORDS):
            full = normalize_url(home_url, href)
            if same_domain(home_url, full):
                candidates.append(full)

    preferred = [c for c in candidates if "press" in c.lower()]
    if preferred:
        return sorted(set(preferred), key=len)[0]
    if candidates:
        return sorted(set(candidates), key=len)[0]
    return None


def is_probable_article_url(u: str) -> bool:
    ul = u.lower()
    path = urlparse(u).path.strip("/")
    if not path:
        return False

    if ul.startswith("mailto:") or ul.startswith("tel:") or "#" in ul:
        return False
    if any(x in ul for x in ("privacy", "terms", "wp-admin", "wp-login")):
        return False

    segments = [s for s in path.split("/") if s]
    if not segments:
        return False

    # Block common section pages (not content)
    section_pages = {
        "meet-your-senator",
        "press-room",
        "press-releases",
        "video",
        "audio",
        "photos",
        "gallery",
        "roads",
        "publications",
        "request-a-congratulatory-certificate",
        "past-email-newsletters",
        "district",
        "contact",
        "donate",
        "subscribe",
        "search",
        "events",
        "resources",
        "bills",
        "media",
        "newsletters",
    }
    if len(segments) == 1 and segments[0].lower() in section_pages:
        return False

    if "page/" in ul or "paged=" in ul:
        return False

    # Strong signal: date in URL
    if re.search(r"/20\d{2}/\d{1,2}/\d{1,2}/", ul):
        return True

    # WordPress id patterns
    if re.search(r"[?&]p=\d+", ul) or re.search(r"[?&]id=\d+", ul):
        return True

    # Hub-like root + slug
    hub_roots = {"press-releases", "press", "news", "blog", "updates"}
    if len(segments) >= 2 and segments[0].lower() in hub_roots:
        slug = segments[-1]
        return len(slug) >= 8

    # Long single-segment slugs often are posts
    if len(segments) == 1 and len(segments[0]) >= 28:
        return True

    return False


def collect_links_from_hub(hub_url: str) -> list[str]:
    html = fetch_html(hub_url)
    soup = BeautifulSoup(html, "lxml")

    links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        full = normalize_url(hub_url, href)
        if same_domain(hub_url, full):
            links.append(full)

    # de-dupe preserving order
    seen = set()
    uniq = []
    for u in links:
        if u in seen:
            continue
        seen.add(u)
        uniq.append(u)

    # remove obvious junk
    junk_patterns = [
        r"#", r"mailto:", r"tel:",
        r"/wp-admin", r"/wp-login", r"/privacy", r"/terms",
        r"/contact", r"/donate", r"/subscribe", r"/search"
    ]

    def is_junk(x: str) -> bool:
        xl = x.lower()
        return any(re.search(p, xl) for p in junk_patterns)

    cleaned = [u for u in uniq if not is_junk(u)]
    return cleaned[:MAX_LINKS_FROM_HUB]


# =========================
# Sitemaps + WP REST fallback
# =========================
def get_urls_from_sitemaps(site_root: str, max_child_sitemaps: int = 15) -> list[str]:
    sitemap_candidates = [
        "/wp-sitemap.xml",
        "/sitemap.xml",
        "/sitemap_index.xml",
        "/sitemap-index.xml",
    ]

    def parse_locs(xml_text: str) -> list[str]:
        root = ET.fromstring(xml_text)
        locs = []
        for elem in root.iter():
            if elem.tag.lower().endswith("loc") and elem.text:
                locs.append(elem.text.strip())
        return locs

    all_urls: list[str] = []

    for path in sitemap_candidates:
        sm_url = urljoin(site_root, path)
        try:
            xml_text = fetch_html(sm_url)
        except Exception as e:
            print(f"Sitemap fetch failed for {sm_url}: {e}")
            continue

        if "<urlset" not in xml_text and "<sitemapindex" not in xml_text:
            continue

        try:
            locs = parse_locs(xml_text)
        except Exception as e:
            print(f"Sitemap parse failed for {sm_url}: {e}")
            continue

        if "sitemapindex" in xml_text:
            for child in locs[:max_child_sitemaps]:
                try:
                    child_xml = fetch_html(child)
                    if "<urlset" in child_xml:
                        all_urls.extend(parse_locs(child_xml))
                except Exception:
                    continue
        else:
            all_urls.extend(locs)

        if all_urls:
            break

    # same-domain only + de-dupe
    seen = set()
    out = []
    for u in all_urls:
        if u in seen:
            continue
        if same_domain(site_root, u):
            seen.add(u)
            out.append(u)
    return out


def wp_rest_search_posts(site_root: str, query: str, per_page: int = 20) -> list[str]:
    api = urljoin(site_root, "/wp-json/wp/v2/posts")
    params = {"per_page": per_page, "search": query}
    try:
        r = requests.get(
            api,
            params=params,
            timeout=25,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json,text/plain,*/*",
            },
        )
        if r.status_code != 200:
            return []
        data = r.json()
        links = []
        for item in data:
            link = item.get("link")
            if link:
                links.append(link)
        return links
    except Exception:
        return []


# =========================
# Legislature Search (Playwright)
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
        page.wait_for_timeout(3000)

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
# Main
# =========================
def main():
    service = get_sheets_service()
    legislators = read_legislators(service)
    seen_urls = read_seenurls(service)

    now = datetime.now(timezone.utc).isoformat()
    print(f"Found {len(legislators)} legislator(s). Already seen {len(seen_urls)} URL(s).")

    new_seen_rows = []
    new_activity_rows = []

    for name, home, hub_override in legislators:
        print(f"\n=== {name} ===")
        print(f"Home: {home}")

        # Pick hub URL
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

        # Collect content links
        try:
            if "legislature.mi.gov/search/executesearch" in hub.lower():
                content_links = collect_legislature_search_result_links(hub)
                print(f"Legislature search collector found {len(content_links)} bill link(s).")
            else:
                links = collect_links_from_hub(hub)
                content_links = [u for u in links if u != hub and is_probable_article_url(u)]

                if not content_links:
                    print("No usable links from hub; trying sitemap fallback...")
                    sitemap_urls = get_urls_from_sitemaps(home)

                    name_slug = urlparse(home).path.strip("/").lower()
                    last_name = name.split()[-1].lower()

                    candidates = []
                    for u in sitemap_urls:
                        ul = u.lower()
                        if name_slug in ul or last_name in ul:
                            if is_probable_article_url(u):
                                candidates.append(u)

                    content_links = candidates[:MAX_LINKS_FROM_HUB]
                    print(f"Sitemap produced {len(content_links)} candidate links.")

                if not content_links:
                    print("Sitemap produced 0; trying WordPress REST API search fallback...")
                    last_name = name.split()[-1]
                    api_links = wp_rest_search_posts(home, last_name, per_page=25)
                    api_links = [u for u in api_links if same_domain(home, u) and is_probable_article_url(u)]
                    content_links = api_links[:MAX_LINKS_FROM_HUB]
                    print(f"REST API produced {len(content_links)} candidate links.")

        except Exception as e:
            print(f"Failed to fetch/parse hub page: {e}")
            continue

        # Canonicalize legislature URLs defensively (even if they slipped in)
        canon_links = []
        for u in content_links:
            if is_legislature_url(u) and "objectname=" in u.lower():
                canon_links.append(canonicalize_legislature_url(u))
            else:
                canon_links.append(u)
        content_links = canon_links

        # Add unseen links to SeenURLs + Activity_Items
        added = 0
        for u in content_links:
            if u in seen_urls:
                continue

            # SeenURLs log
            new_seen_rows.append([u, name, now])
            seen_urls.add(u)

            # Activity_Items row (blank title/summary/tags for analyzer to fill later)
            if is_legislature_url(u) and "objectname=" in u.lower():
                source_type = "bill"
            else:
                source_type = "press"

            new_activity_rows.append([u, name, source_type, now, "", "", ""])
            added += 1

        print(f"Collected {len(content_links)} candidate links; added {added} new.")

    # Write outputs
    sheets_append_values(service, SEENURLS_RANGE_APPEND, new_seen_rows)
    sheets_append_values(service, ACTIVITY_RANGE_APPEND, new_activity_rows)

    print(f"\nAppended {len(new_seen_rows)} new URL(s) to SeenURLs.")
    print(f"Appended {len(new_activity_rows)} new row(s) to Activity_Items.")


if __name__ == "__main__":
    main()
