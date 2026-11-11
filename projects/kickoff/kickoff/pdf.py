"""The signed PDF: the agreement exactly as signed, then a signature certificate page with who, when, from where,
and the document's SHA-256, so anyone can check the text wasn't changed afterwards."""
from pathlib import Path

from fpdf import FPDF


def latin(s: str) -> str:
    # shortcut: the built-in PDF fonts are Latin-1 only; embed a TTF font for names in other scripts
    return s.replace("€", "EUR ").replace("…", "...").replace("—", "-").encode("latin-1", "replace").decode("latin-1")


def render(text: str, record: dict, path: Path) -> Path:
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(True, margin=18)
    pdf.set_title("Signed agreement")
    pdf.add_page()
    lines = text.splitlines()
    pdf.set_font("Helvetica", "B", 15)
    pdf.multi_cell(0, 8, latin(lines[0]), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    for line in lines[1:]:
        heading = line.isupper() and line.strip()
        pdf.set_font("Helvetica", "B" if heading else "", 11 if heading else 10)
        pdf.multi_cell(0, 6, latin(line) or " ", new_x="LMARGIN", new_y="NEXT")
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "Signature certificate", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    for label, key in [("Signed by (typed name)", "name"), ("Email", "email"), ("Signed at", "at"), ("IP address", "ip"),
                       ("Browser", "user_agent"), ("Document SHA-256", "sha256")]:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(48, 7, label)
        pdf.set_font("Courier" if key == "sha256" else "Helvetica", "", 9 if key in ("sha256", "user_agent") else 10)
        pdf.multi_cell(0, 7, latin(str(record[key])), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 9)
    pdf.multi_cell(0, 5, "The fingerprint above is the SHA-256 of the agreement text on the previous pages, exactly as it was shown "
                         "to the signer. The signer saw the same fingerprint on the signing page.")
    pdf.output(str(path))
    return path
