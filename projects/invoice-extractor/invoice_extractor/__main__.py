import argparse, csv
from pathlib import Path

from .core import process

HEAD = ["file", "vendor", "number", "date", "currency", "subtotal", "tax", "total"]


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract invoice PDFs to CSV; invoices that fail checks go to review.csv")
    ap.add_argument("input", type=Path, help="folder with PDF invoices")
    ap.add_argument("--out", type=Path, default=Path("out"))
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    invoices = [process(p) for p in sorted(args.input.glob("*.pdf"))]
    ok = [i for i in invoices if not i.problems]
    bad = [i for i in invoices if i.problems]

    with open(args.out / "invoices.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEAD)
        w.writerows([getattr(i, h) for h in HEAD] for i in ok)
    with open(args.out / "line_items.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["invoice", "description", "qty", "unit_price", "amount"])
        w.writerows([i.number, *l.values()] for i in ok for l in i.lines)
    with open(args.out / "review.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["file", "problems"])
        w.writerows([i.file, "; ".join(i.problems)] for i in bad)
    print(f"{len(ok)} invoices extracted, {len(bad)} sent to review -> {args.out}/")


if __name__ == "__main__":
    main()
