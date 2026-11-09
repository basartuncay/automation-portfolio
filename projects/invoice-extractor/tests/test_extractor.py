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
