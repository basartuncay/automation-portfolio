"""Extract structured data from vendor invoices and check that the numbers add up."""
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pdfplumber


def us_num(s: str) -> float:
    return float(s.replace(",", ""))


def eu_num(s: str) -> float:  # "1 234,50" -> 1234.5
    return float(s.replace(" ", "").replace(" ", "").replace(".", "").replace(",", "."))


# One entry per vendor layout. Adding a new vendor = adding one entry here.
TEMPLATES = {
    "acme": {
        "detect": "ACME Supplies Ltd",
        "number": r"Invoice Number:\s*(\S+)",
        "date": (r"Invoice Date:\s*(\d{4}-\d{2}-\d{2})", "%Y-%m-%d"),
        "currency": r"Total Due:\s*[\d.,]+\s*([A-Z]{3})",
        "subtotal": r"Subtotal:\s*([\d.,]+)",
        "tax": r"Tax \([^)]*\):\s*([\d.,]+)",
        "total": r"Total Due:\s*([\d.,]+)",
        "line": r"^(.+?) \| (\d+) \| ([\d.,]+) \| ([\d.,]+)$",
        "num": us_num,
    },
    "nordic": {
        "detect": "Nordic Parts AB",
        "number": r"Bill No\.\s*(\S+)",
        "date": (r"Date:\s*(\d{2}\.\d{2}\.\d{4})", "%d.%m.%Y"),
        "currency": r"Grand total\s*([A-Z]{3})",
        "subtotal": r"Net amount [A-Z]{3}\s*([\d ,]+)",
        "tax": r"VAT [^A-Z]*[A-Z]{3}\s*([\d ,]+)",
        "total": r"Grand total [A-Z]{3}\s*([\d ,]+)",
        "line": r"^(.+?);(\d+);([\d ,]+);([\d ,]+)$",
        "num": eu_num,
    },
}


@dataclass
class Invoice:
    file: str
    vendor: str | None = None
    number: str | None = None
    date: str | None = None
    currency: str | None = None
    subtotal: float | None = None
    tax: float | None = None
    total: float | None = None
    lines: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def extract_text(path: Path) -> str:
    with pdfplumber.open(path) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def parse(text: str, file: str) -> Invoice:
    inv = Invoice(file=file)
    name, t = next(((n, t) for n, t in TEMPLATES.items() if t["detect"] in text), (None, None))
    if not t:
        inv.problems.append("unknown layout: add a template")
        return inv
    inv.vendor = t["detect"]

    def grab(key, conv=lambda s: s.strip()):
        m = re.search(t[key], text)
        if not m:
            inv.problems.append(f"missing {key}")
            return None
        try:
            return conv(m.group(1))
        except ValueError:
            inv.problems.append(f"unreadable {key}: {m.group(1)!r}")
            return None

    inv.number, inv.currency = grab("number"), grab("currency")
    pattern, fmt = t["date"]
    m = re.search(pattern, text)
    inv.date = datetime.strptime(m.group(1), fmt).date().isoformat() if m else None
    if not m:
        inv.problems.append("missing date")
    inv.subtotal, inv.tax, inv.total = (grab(k, t["num"]) for k in ("subtotal", "tax", "total"))

    for line in text.splitlines():
        if lm := re.match(t["line"], line.strip()):
            desc, qty, price, amount = lm.groups()
            row = {"description": desc.strip(), "qty": int(qty), "unit_price": t["num"](price), "amount": t["num"](amount)}
            if abs(row["qty"] * row["unit_price"] - row["amount"]) > 0.01:
                inv.problems.append(f"line math off: {desc}")
            inv.lines.append(row)
    validate(inv)
    return inv


def validate(inv: Invoice) -> None:
    if not inv.lines:
        inv.problems.append("no line items found")
    if inv.subtotal is not None and inv.lines and abs(sum(l["amount"] for l in inv.lines) - inv.subtotal) > 0.01:
        inv.problems.append("line items do not sum to subtotal")
    if None not in (inv.subtotal, inv.tax, inv.total) and abs(inv.subtotal + inv.tax - inv.total) > 0.01:
        inv.problems.append("subtotal + tax != total")


def process(path: Path) -> Invoice:
    try:
        return parse(extract_text(path), path.name)
    except Exception as e:  # a corrupt PDF must not stop the batch
        return Invoice(file=path.name, problems=[f"could not read PDF: {e}"])
