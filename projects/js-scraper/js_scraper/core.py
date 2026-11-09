"""Two ways to scrape a JavaScript-rendered site:

1. browser: render each page in headless Chromium (Playwright) and read the DOM.
2. api: most JS sites load their data from a JSON endpoint; call it directly (10-100x faster, no browser).
`discover` opens a page in the browser and lists the JSON endpoints it calls, to find option 2.
"""
import json, logging, time
from dataclasses import dataclass, field
from urllib.parse import urljoin

import httpx
from selectolax.lexbor import LexborHTMLParser

log = logging.getLogger("js_scraper")
BASE = "https://quotes.toscrape.com"


@dataclass
class Quote:
    text: str
    author: str
    tags: list[str] = field(default_factory=list)


def parse_rendered(html: str) -> tuple[list[Quote], bool]:
    """Quotes from a rendered page, plus whether a next page exists."""
    tree = LexborHTMLParser(html)
    quotes = []
    for q in tree.css("div.quote"):
        text, author = q.css_first("span.text"), q.css_first("small.author")
        if not text or not author:
            continue
        tags_el = q.css_first("div.tags")
        tags = [a.text(strip=True) for a in q.css("div.tags a.tag")]
        if not tags and tags_el:  # the JS version writes tags as plain text: "Tags: a,b"
            raw = tags_el.text(strip=True).removeprefix("Tags:").strip()
            tags = [t.strip() for t in raw.split(",") if t.strip()]
        quotes.append(Quote(text.text(strip=True), author.text(strip=True), tags))
    return quotes, tree.css_first("li.next a") is not None


def from_api(item: dict) -> Quote:
    return Quote(item["text"], item["author"]["name"], item.get("tags", []))


def scrape_api(client: httpx.Client, max_pages: int = 100, delay: float = 0.3) -> list[Quote]:
    quotes, page = [], 1
    while page <= max_pages:
        r = client.get(f"{BASE}/api/quotes", params={"page": page})
        r.raise_for_status()
        data = r.json()
        quotes += [from_api(i) for i in data["quotes"]]
        if not data.get("has_next"):
            break
        page += 1
        time.sleep(delay)
    return quotes


def scrape_browser(start: str = f"{BASE}/js/", max_pages: int = 100, headless: bool = True) -> list[Quote]:
    from playwright.sync_api import sync_playwright

    quotes, url = [], start
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(user_agent="js-scraper/1.0")
        page.route("**/*.{png,jpg,jpeg,gif,svg,woff,woff2}", lambda route: route.abort())  # skip assets: faster
        for _ in range(max_pages):
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_selector("div.quote", timeout=15000)
            found, has_next = parse_rendered(page.content())
            quotes += found
            if not has_next:
                break
            url = urljoin(url, page.get_attribute("li.next a", "href"))
        browser.close()
    return quotes


def discover(url: str, scrolls: int = 3) -> list[str]:
    """Open a page, scroll, and return the JSON endpoints it requested."""
    from playwright.sync_api import sync_playwright

    seen = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on("response", lambda r: seen.append(r.url)
                if "json" in (r.headers.get("content-type") or "") and r.url not in seen else None)
        page.goto(url, wait_until="networkidle")
        for _ in range(scrolls):
            page.mouse.wheel(0, 20000)
            page.wait_for_timeout(800)
        browser.close()
    return seen


def dedupe(quotes: list[Quote]) -> list[Quote]:
    seen, out = set(), []
    for q in quotes:
        if (q.text, q.author) not in seen:
            seen.add((q.text, q.author))
            out.append(q)
    return out


def to_jsonl(quotes: list[Quote]) -> str:
    return "\n".join(json.dumps(q.__dict__, ensure_ascii=False) for q in quotes)
