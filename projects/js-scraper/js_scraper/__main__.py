import argparse, logging, time
from pathlib import Path

import httpx

from .core import dedupe, discover, scrape_api, scrape_browser, to_jsonl


def main() -> None:
    ap = argparse.ArgumentParser(description="Scrape a JS-rendered site via headless browser or its JSON API")
    ap.add_argument("mode", choices=["browser", "api", "discover"])
    ap.add_argument("--url", default="https://quotes.toscrape.com/scroll", help="page to inspect (discover mode)")
    ap.add_argument("--max-pages", type=int, default=100)
    ap.add_argument("--out", type=Path, default=Path("quotes.jsonl"))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)

    if args.mode == "discover":
        for u in discover(args.url):
            print(u)
        return
    t0 = time.time()
    if args.mode == "api":
        with httpx.Client(headers={"User-Agent": "js-scraper/1.0"}, timeout=30) as c:
            quotes = scrape_api(c, args.max_pages)
    else:
        quotes = scrape_browser(max_pages=args.max_pages)
    quotes = dedupe(quotes)
    args.out.write_text(to_jsonl(quotes))
    print(f"{len(quotes)} quotes in {time.time() - t0:.1f}s via {args.mode} -> {args.out}")


if __name__ == "__main__":
    main()
