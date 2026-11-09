import argparse, json, logging, os
from dataclasses import asdict
from pathlib import Path

from .core import crawl, diff, write_outputs


def push_to_sheets(sheet_id: str, products, changes) -> None:
    import gspread  # optional extra: pip install .[sheets]

    gc = gspread.service_account(filename=os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
    sh = gc.open_by_key(sheet_id)
    for title, rows in (("products", [asdict(p) for p in products]), ("changes", changes)):
        try:
            ws = sh.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title, rows=1, cols=10)
        ws.clear()
        if rows:
            ws.update([list(rows[0])] + [[str(v) for v in r.values()] for r in rows])


def main() -> None:
    ap = argparse.ArgumentParser(description="Crawl a catalogue and report price/stock changes.")
    ap.add_argument("--start-url", default="https://books.toscrape.com/catalogue/page-1.html")
    ap.add_argument("--max-pages", type=int, default=50)
    ap.add_argument("--out", type=Path, default=Path("data"))
    ap.add_argument("--min-products", type=int, default=1, help="fail the run if fewer rows came back (layout change alarm)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    snap = args.out / "snapshot.json"
    old = json.loads(snap.read_text()) if snap.exists() else {}
    products, errors = crawl(args.start_url, args.max_pages)
    if len(products) < args.min_products:
        raise SystemExit(f"only {len(products)} products scraped; the site layout may have changed")
    changes = diff(old, products)
    write_outputs(args.out, products, changes, errors)
    if sheet_id := os.environ.get("GOOGLE_SHEET_ID"):
        push_to_sheets(sheet_id, products, changes)
    print(f"{len(products)} products, {len(changes)} changes, {len(errors)} rejected rows -> {args.out}/")


if __name__ == "__main__":
    main()
