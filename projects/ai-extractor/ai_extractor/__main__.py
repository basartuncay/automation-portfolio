import argparse, json, time
from dataclasses import asdict
from pathlib import Path

import anthropic
import httpx

from .core import cost, extract, strictify


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract JSON from web pages with Claude, following a JSON schema")
    ap.add_argument("schema", type=Path, help="JSON schema file (type: object)")
    ap.add_argument("urls", nargs="+", help="page URLs, or @file.txt with one URL per line")
    ap.add_argument("--instructions", default="", help="extra guidance, e.g. 'prices in EUR'")
    ap.add_argument("--out", type=Path, default=Path("results.jsonl"))
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between page fetches")
    args = ap.parse_args()

    urls = [u for a in args.urls for u in (Path(a[1:]).read_text().split() if a.startswith("@") else [a])]
    schema = strictify(json.loads(args.schema.read_text()))
    results = []
    with httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": "ai-extractor/1.0"}) as http:
        client = anthropic.Anthropic()
        for i, url in enumerate(urls):
            r = extract(client, http, url, schema, args.instructions)
            results.append(r)
            print(("OK   " if r.data else "FAIL ") + url + (f"  ({r.error})" if r.error else ""))
            if i < len(urls) - 1:
                time.sleep(args.delay)
    args.out.write_text("\n".join(json.dumps(asdict(r), ensure_ascii=False) for r in results) + "\n")
    ok = sum(1 for r in results if r.data)
    print(f"{ok}/{len(results)} extracted, est. cost ${cost(results):.4f} -> {args.out}")


if __name__ == "__main__":
    main()
