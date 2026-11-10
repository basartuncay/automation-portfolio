"""Opt-in: run the watcher against a real public website (books.toscrape.com, a sandbox built for scraping practice).
LIVE=1 uv run pytest tests/test_live.py -q"""
import os
from pathlib import Path

import httpx
import pytest

from price_watch import db as store
from price_watch.report import positions
from price_watch.watch import run_all

LIVE = Path(__file__).parents[1] / "samples" / "live"
pytestmark = pytest.mark.skipif(os.environ.get("LIVE") != "1", reason="live network test; set LIVE=1")


def test_real_site_crawl_baseline_and_positions(tmp_path, monkeypatch):
    monkeypatch.setenv("CRAWL_DELAY", "0.5")
    conn = store.connect(str(tmp_path / "live.db"))
    store.load_config(conn, LIVE / "competitors.json", LIVE / "our_catalog.csv")
    with httpx.Client(headers={"User-Agent": "price-watch/1.0 (portfolio test)"}, timeout=30, follow_redirects=True) as client:
        first = run_all(conn, client, None)
        second = run_all(conn, client, None)
    assert first[0]["status"] == "ok" and first[0]["products"] >= 8 and first[0]["errors"] == 0
    assert second[0]["status"] == "ok" and second[0]["changes"] == 0  # a static catalogue: re-crawl changes nothing
    assert conn.execute("select count(*) from alerts").fetchone()[0] == 0
    assert {r["currency"] for r in conn.execute("select currency from products")} == {"GBP"}
    pos = {p["sku"]: p for p in positions(conn)}
    assert pos["BK-001"]["gap_pct"] > 0  # we priced it above the site: undercut
    assert pos["BK-002"]["gap_pct"] < 0  # below the site: cheapest
    assert all(p["cheapest"] for p in pos.values()), "every catalogue product was found on the real site"
