"""Generate synthetic invoices in two different layouts (no real customer data)."""
import sys
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


def draw(path, lines):
    c = canvas.Canvas(str(path), pagesize=A4)
    y = 800
    for text in lines:
        c.drawString(50, y, text)
        y -= 18
    c.save()


def scan(src, dst):
    """An image-only copy of a PDF, like a scanner produces: no text layer at all."""
    import pdfplumber

    with pdfplumber.open(src) as pdf:
        pdf.pages[0].to_image(resolution=200).original.convert("L").save(dst, "PDF", resolution=200)


def acme(n, items, tax_rate=0.20, broken=False):
    sub = round(sum(q * p for _, q, p in items), 2)
    tax = round(sub * tax_rate, 2)
    total = round(sub + tax + (5 if broken else 0), 2)  # broken: total doesn't add up
    return ["ACME Supplies Ltd", f"Invoice Number: INV-{n:05d}", "Invoice Date: 2026-10-14", "",
            "Description | Qty | Unit Price | Amount"] + \
           [f"{d} | {q} | {p:.2f} | {q * p:.2f}" for d, q, p in items] + \
           ["", f"Subtotal: {sub:.2f} USD", f"Tax (20%): {tax:.2f} USD", f"Total Due: {total:.2f} USD"]


def nordic(n, items):
    sub = round(sum(q * p for _, q, p in items), 2)
    vat = round(sub * 0.25, 2)
    fmt = lambda v: f"{v:,.2f}".replace(",", " ").replace(".", ",")  # 1 234,50 style
    return ["Nordic Parts AB", f"Bill No. NP/{n}", "Date: 03.11.2026", "",
            "Item;Quantity;Price;Line total"] + \
           [f"{d};{q};{fmt(p)};{fmt(q * p)}" for d, q, p in items] + \
           ["", f"Net amount EUR {fmt(sub)}", f"VAT 25% EUR {fmt(vat)}", f"Grand total EUR {fmt(sub + vat)}"]


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "samples")
    out.mkdir(exist_ok=True)
    draw(out / "acme_1.pdf", acme(1042, [("Printer paper A4", 10, 4.5), ("Toner cartridge", 2, 61.99)]))
    draw(out / "acme_2_broken.pdf", acme(1043, [("Stapler", 3, 12.0)], broken=True))
    draw(out / "nordic_1.pdf", nordic(7781, [("Hydraulic hose", 4, 1250.5), ("Seal kit", 10, 39.9)]))
    draw(out / "unknown_vendor.pdf", ["Some Other Company", "Receipt 55", "Total 10.00"])
    (out / "scanned").mkdir(exist_ok=True)  # kept apart so batch demos and the n8n test see the same 4 files
    scan(out / "acme_1.pdf", out / "scanned" / "acme_1_scanned.pdf")
    print(f"wrote samples to {out}/")
