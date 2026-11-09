import argparse, csv, json, logging, os
from pathlib import Path

from .core import Airtable, read_rows, transform


def main() -> None:
    ap = argparse.ArgumentParser(description="Load a CSV/XLSX into Airtable: validate, dedupe, upsert by key")
    ap.add_argument("file", type=Path)
    ap.add_argument("--mapping", type=Path, default=Path("mapping.json"))
    ap.add_argument("--rejects", type=Path, default=Path("rejects.csv"))
    ap.add_argument("--dry-run", action="store_true", help="validate and show what would be sent; send nothing")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    mapping = json.loads(args.mapping.read_text())
    res = transform(read_rows(args.file), mapping)
    if res.rejects:
        with open(args.rejects, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(res.rejects[0]))
            w.writeheader()
            w.writerows(res.rejects)
    print(f"{len(res.records)} valid records, {len(res.rejects)} rejected (-> {args.rejects}), "
          f"{res.duplicates} duplicates merged")
    if args.dry_run:
        for r in res.records[:5]:
            print("  would upsert:", r)
        return
    at = Airtable(os.environ["AIRTABLE_BASE_ID"], mapping["table"], os.environ["AIRTABLE_TOKEN"])
    created, updated = at.upsert(res.records, mapping["key"])
    print(f"Airtable: {created} created, {updated} updated")


if __name__ == "__main__":
    main()
