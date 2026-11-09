import json
from pathlib import Path

import httpx
import pytest

from js_scraper import core
from js_scraper.core import Quote, dedupe, parse_rendered, scrape_api

FIX = Path(__file__).parent / "fixtures"


def test_parse_rendered_js_page():
    quotes, has_next = parse_rendered((FIX / "js_page_rendered.html").read_text())
    assert len(quotes) == 10 and has_next
    assert quotes[0].author == "Albert Einstein"
    assert quotes[0].tags == ["change", "deep-thoughts", "thinking", "world"]


def test_unrendered_page_yields_nothing():
    # The raw HTML of a JS site has no quotes yet; this is why a browser (or the API) is needed.
    raw = '<html><body><script>var data=[{"text":"x"}]; document.write("...")</script></body></html>'
    assert parse_rendered(raw) == ([], False)


def test_api_mode_follows_pages_until_has_next_false(monkeypatch):
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    page1 = json.loads((FIX / "api_page1.json").read_text())
    pages = {"1": page1, "2": {**page1, "has_next": False, "quotes": page1["quotes"][:2]}}
    seen = []

    def handler(req):
        p = req.url.params["page"]
        seen.append(p)
        return httpx.Response(200, json=pages[p])

    quotes = scrape_api(httpx.Client(transport=httpx.MockTransport(handler)))
    assert seen == ["1", "2"] and len(quotes) == 12
    assert quotes[0] == Quote(page1["quotes"][0]["text"], "Albert Einstein", page1["quotes"][0]["tags"])


def test_api_error_is_raised_not_swallowed():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(httpx.HTTPStatusError):
        scrape_api(client)


def test_dedupe():
    a = Quote("t", "a")
    assert dedupe([a, Quote("t", "a"), Quote("t", "b")]) == [a, Quote("t", "b")]


@pytest.mark.live
def test_browser_and_api_agree_live():
    browser = dedupe(core.scrape_browser(max_pages=2))
    with httpx.Client(timeout=30) as c:
        api = dedupe(scrape_api(c, max_pages=2))
    assert browser == api and len(api) == 20


@pytest.mark.live
def test_discover_finds_hidden_api_live():
    assert any("/api/quotes" in u for u in core.discover("https://quotes.toscrape.com/scroll"))
