import subprocess, sys
from pathlib import Path

import pytest

from invoice_extractor.core import eu_num, process

ROOT = Path(__file__).parents[1]


@pytest.fixture(scope="session")
def samples(tmp_path_factory):
    out = tmp_path_factory.mktemp("samples")
    subprocess.run([sys.executable, str(ROOT / "make_samples.py"), str(out)], check=True, capture_output=True)
    return out


def test_us_layout(samples):
    inv = process(samples / "acme_1.pdf")
    assert inv.problems == []
    assert (inv.number, inv.date, inv.currency) == ("INV-01042", "2026-10-14", "USD")
    assert (inv.subtotal, inv.tax, inv.total) == (168.98, 33.8, 202.78)
    assert inv.lines[1] == {"description": "Toner cartridge", "qty": 2, "unit_price": 61.99, "amount": 123.98}


def test_eu_layout_with_comma_decimals(samples):
    inv = process(samples / "nordic_1.pdf")
    assert inv.problems == []
    assert (inv.number, inv.date, inv.currency) == ("NP/7781", "2026-11-03", "EUR")
    assert inv.subtotal == 5401.0 and inv.total == 6751.25


def test_totals_that_do_not_add_up_go_to_review(samples):
    assert "subtotal + tax != total" in process(samples / "acme_2_broken.pdf").problems


def test_unknown_vendor_goes_to_review(samples):
    assert process(samples / "unknown_vendor.pdf").problems == ["unknown layout: add a template"]


def test_corrupt_pdf_does_not_crash(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    assert process(bad).problems[0].startswith("could not read PDF")


def test_eu_number_format():
    assert eu_num("1 234,50") == 1234.5 and eu_num("39,90") == 39.9


import shutil  # noqa: E402

from invoice_extractor import core  # noqa: E402


def test_scanned_pdf_has_no_text_layer(samples):
    import pdfplumber

    with pdfplumber.open(samples / "scanned" / "acme_1_scanned.pdf") as pdf:
        assert not (pdf.pages[0].extract_text() or "").strip()


def test_scanned_pdf_goes_through_ocr(samples, monkeypatch):
    text_layer = core.extract_text(samples / "acme_1.pdf")[0]
    monkeypatch.setattr(core, "ocr_pages", lambda pdf: text_layer)  # stand-in for Tesseract
    inv = process(samples / "scanned" / "acme_1_scanned.pdf")
    assert inv.ocr is True and inv.problems == [] and inv.total == 202.78


def test_scanned_pdf_without_ocr_installed_goes_to_review(samples, monkeypatch):
    def missing(pdf):
        raise RuntimeError("scanned PDF and OCR is not installed")

    monkeypatch.setattr(core, "ocr_pages", missing)
    assert "OCR is not installed" in process(samples / "scanned" / "acme_1_scanned.pdf").problems[0]


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="needs the tesseract binary (runs in Docker/CI)")
def test_real_ocr_reads_the_scanned_invoice(samples):
    inv = process(samples / "scanned" / "acme_1_scanned.pdf")
    assert inv.ocr is True
    assert (inv.number, inv.total) == ("INV-01042", 202.78), inv.problems
