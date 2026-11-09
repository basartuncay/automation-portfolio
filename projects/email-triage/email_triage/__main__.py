import argparse, csv
from pathlib import Path

import anthropic

from .core import read_eml, route, triage

FIELDS = ["file", "from", "subject", "route", "category", "urgency", "summary", "company",
          "requested_action", "amount", "currency", "due_date", "needs_reply"]


def run(client, inbox: Path, out: Path) -> tuple[int, int]:
    out.mkdir(parents=True, exist_ok=True)
    done = review = 0
    with open(out / "triaged.csv", "w", newline="") as f, open(out / "review.csv", "w", newline="") as r:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        rw = csv.writer(r)
        w.writeheader()
        rw.writerow(["file", "from", "subject", "problem"])
        for path in sorted(inbox.glob("*.eml")):
            mail = read_eml(path)
            result, problem = triage(client, mail)
            if problem:
                rw.writerow([mail["file"], mail["from"], mail["subject"], problem])
                review += 1
                continue
            row = {k: mail[k] for k in ("file", "from", "subject")} | result.model_dump(mode="json")
            row["route"] = route(result)
            w.writerow({k: row.get(k) for k in FIELDS})
            done += 1
    return done, review


def main() -> None:
    ap = argparse.ArgumentParser(description="Triage a folder of .eml files with Claude")
    ap.add_argument("inbox", type=Path)
    ap.add_argument("--out", type=Path, default=Path("out"))
    args = ap.parse_args()
    done, review = run(anthropic.Anthropic(), args.inbox, args.out)
    print(f"{done} emails triaged, {review} sent to review -> {args.out}/")


if __name__ == "__main__":
    main()
