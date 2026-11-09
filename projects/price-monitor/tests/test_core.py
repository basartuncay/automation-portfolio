from pathlib import Path

import httpx
import pytest

from price_monitor import core
from price_monitor.core import Product, diff, fetch, parse_list

FIX = Path(__file__).parent / "fixtures"
PAGE = "https://books.toscrape.com/catalogue/page-1.html"


def test_parse_list_real_page():
    products, errors, nxt = parse_list((FIX / "list_page.html").read_text(), PAGE)
    assert len(products) == 20 and not errors
    first = products[0]
    assert first.title == "A Light in the Attic"
    assert first.price == 51.77 and first.currency == "GBP"
    assert first.rating == 3 and first.in_stock
    assert first.id == "a-light-in-the-attic_1000"
    assert nxt == "https://books.toscrape.com/catalogue/page-2.html"


def test_bad_rows_are_rejected_not_crashed():
    html = '<article class="product_pod"><h3><a href="x/index.html" title="T">T</a></h3><p class="price_color">£0</p></article>'
    products, errors, nxt = parse_list(html, PAGE)
    assert products == [] and len(errors) == 1 and nxt is None


def p(pid, price, stock=True):
    return Product(pid, pid.title(), price, "GBP", 3, stock, f"u/{pid}")


def test_diff_detects_all_change_types():
    old = {"a": {"price": 10.0, "in_stock": True, "title": "A"}, "gone": {"price": 5.0, "in_stock": True, "title": "G"}}
    changes = {(c["id"], c["change"]) for c in diff(old, [p("a", 12.0, stock=False), p("b", 3.0)])}
    assert changes == {("a", "price"), ("a", "stock"), ("b", "new"), ("gone", "removed")}


def test_fetch_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503 if len(calls) < 3 else 200, text="ok")

    assert fetch(httpx.Client(transport=httpx.MockTransport(handler)), "http://x") == "ok"
    assert len(calls) == 3


def test_fetch_does_not_retry_404(monkeypatch):
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(404)

    with pytest.raises(httpx.HTTPStatusError):
        fetch(httpx.Client(transport=httpx.MockTransport(handler)), "http://x")
    assert len(calls) == 1
