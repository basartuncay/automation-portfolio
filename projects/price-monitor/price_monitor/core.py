"""Price monitor: crawl a catalogue, validate rows, detect price/stock changes since the last run."""
import csv, json, logging, time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urljoin

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser

log = logging.getLogger("price_monitor")
RATINGS = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}


@dataclass
class Product:
    id: str
    title: str
    price: float
    currency: str
    rating: int | None
    in_stock: bool
    url: str


def parse_list(html: str, page_url: str) -> tuple[list[Product], list[str], str | None]:
    """Return (valid products, validation errors, next page url)."""
    tree = HTMLParser(html)
    products, errors = [], []
    for pod in tree.css("article.product_pod"):
        link = pod.css_first("h3 a")
        price_el = pod.css_first("p.price_color")
        rating_el = pod.css_first("p.star-rating")
        try:
            url = urljoin(page_url, link.attributes["href"])
            raw_price = price_el.text(strip=True)
            price = float(raw_price.lstrip("£$€").replace(",", ""))
            title = (link.attributes.get("title") or link.text()).strip()
            if not title or price <= 0:
                raise ValueError(f"bad title/price: {title!r} {raw_price!r}")
        except (AttributeError, KeyError, ValueError) as e:
            errors.append(f"{page_url}: {e}")
            continue
        rating_cls = rating_el.attributes.get("class", "").split() if rating_el else []
        products.append(Product(
            id=url.rstrip("/").split("/")[-2] if url.endswith("index.html") else url,
            title=title,
            price=price,
            currency={"£": "GBP", "$": "USD", "€": "EUR"}.get(raw_price[:1], "GBP"),
            rating=next((RATINGS[c] for c in rating_cls if c in RATINGS), None),
            in_stock="in stock" in (pod.css_first("p.availability").text().lower() if pod.css_first("p.availability") else ""),
            url=url,
        ))
    nxt = tree.css_first("li.next a")
    return products, errors, urljoin(page_url, nxt.attributes["href"]) if nxt else None


def fetch(client: httpx.Client, url: str, retries: int = 3, backoff: float = 1.0) -> str:
    for attempt in range(1, retries + 1):
        try:
            r = client.get(url)
            if r.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError(f"retryable {r.status_code}", request=r.request, response=r)
            r.raise_for_status()
            return r.text
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            if attempt == retries or (isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 429):
                raise
            log.warning("retry %s/%s for %s: %s", attempt, retries, url, e)
            time.sleep(backoff * 2 ** (attempt - 1))


def crawl(start_url: str, max_pages: int = 50, delay: float = 0.5, client: httpx.Client | None = None):
    client = client or httpx.Client(headers={"User-Agent": "price-monitor/1.0"}, timeout=30, follow_redirects=True)
    seen, products, errors, url, pages = set(), [], [], start_url, 0
    while url and pages < max_pages:
        items, errs, url = parse_list(fetch(client, url), url)
        errors += errs
        for p in items:
            if p.id not in seen:  # dedupe across pages
                seen.add(p.id)
                products.append(p)
        pages += 1
        if url:
            time.sleep(delay)  # be polite
    log.info("crawled %s pages, %s products, %s errors", pages, len(products), len(errors))
    return products, errors


def diff(old: dict[str, dict], new: list[Product]) -> list[dict]:
    """Changes since last snapshot: new, removed, price, stock."""
    changes, now = [], {p.id: p for p in new}
    for pid, p in now.items():
        prev = old.get(pid)
        if prev is None:
            changes.append({"id": pid, "change": "new", "old": None, "new": p.price, "title": p.title})
            continue
        if prev["price"] != p.price:
            changes.append({"id": pid, "change": "price", "old": prev["price"], "new": p.price, "title": p.title})
        if prev["in_stock"] != p.in_stock:
            changes.append({"id": pid, "change": "stock", "old": prev["in_stock"], "new": p.in_stock, "title": p.title})
    for pid in old.keys() - now.keys():
        changes.append({"id": pid, "change": "removed", "old": old[pid]["price"], "new": None, "title": old[pid]["title"]})
    return changes


def write_outputs(out: Path, products: list[Product], changes: list[dict], errors: list[str]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    rows = [asdict(p) for p in products]
    (out / "snapshot.json").write_text(json.dumps({r["id"]: r for r in rows}, indent=1))
    for name, data in (("products.csv", rows), ("changes.csv", changes)):
        with open(out / name, "w", newline="") as f:
            if data:
                w = csv.DictWriter(f, fieldnames=list(data[0]))
                w.writeheader()
                w.writerows(data)
    (out / "errors.log").write_text("\n".join(errors))
