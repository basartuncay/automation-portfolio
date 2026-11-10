import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from alert_bot.core import Notifier
from price_watch import db as store
from price_watch.demo_shop import create_app as shop_app
from price_watch.report import build_digest, positions, send_digest
from price_watch.watch import notify_pending, run_all
from price_watch.web import create_app

ROOT = Path(__file__).parents[1]
SAMPLES = ROOT / "samples"
TOKEN = "t" * 32
HEADPHONES = "wireless-headphones-x200"  # SKU-1001, ours €128.99


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CRAWL_DELAY", "0")
    shop = TestClient(shop_app(str(SAMPLES / "shop_catalog.json"), TOKEN), base_url="http://shop:8000")
    conn = store.connect(str(tmp_path / "w.db"))
    store.load_config(conn, SAMPLES / "competitors.json", SAMPLES / "our_catalog.csv")
    yield shop, conn, str(tmp_path / "w.db")
    conn.close()


def setp(shop, store_, pid, **kw):
    assert shop.post(f"/{store_}/admin/set", json={"pid": pid, **kw}, headers={"x-token": TOKEN}).status_code == 200


def alerts(conn):
    return [(a["kind"], a["competitor"], a["pid"]) for a in conn.execute("select * from alerts order by id")]


def test_first_crawl_is_a_silent_baseline(env):
    shop, conn, _ = env
    res = run_all(conn, shop, None)
    assert [(r["competitor"], r["status"], r["products"]) for r in res] == [("GadgetHub", "ok", 24), ("VoltMart", "ok", 24)]
    assert alerts(conn) == []
    assert conn.execute("select count(*) from prices").fetchone()[0] == 48
    assert all(p["gap_pct"] < 0 for p in positions(conn))  # the sample starts with us cheapest everywhere


def test_changes_raise_the_right_alerts_only_for_our_products(env):
    shop, conn, _ = env
    run_all(conn, shop, None)
    setp(shop, "voltmart", HEADPHONES, price=119.00)                       # below our 128.99 -> undercut
    setp(shop, "gadgethub", "27-inch-4k-monitor", price=300.00)            # -14% but still above ours? ours 328.99 -> undercut
    setp(shop, "gadgethub", "portable-ssd-1tb", price=111.0)               # small change above ours -> no alert
    setp(shop, "gadgethub", "usb-c-charger-65w", in_stock=False)            # -> stock
    setp(shop, "voltmart", "webcam-1080p", listed=False)                    # -> delisted
    setp(shop, "voltmart", "tripod-flex", price=5.00)                       # not a product we sell -> recorded, no alert
    run_all(conn, shop, None)
    got = alerts(conn)
    assert ("undercut", "VoltMart", HEADPHONES) in got and ("undercut", "GadgetHub", "27-inch-4k-monitor") in got
    assert ("stock", "GadgetHub", "usb-c-charger-65w") in got and ("stock", "VoltMart", "webcam-1080p") in got
    assert not any(pid in ("portable-ssd-1tb", "tripod-flex") for _, _, pid in got)
    assert len(got) == 4
    msg = conn.execute("select message from alerts where pid = ?", (HEADPHONES,)).fetchone()[0]
    assert msg == "VoltMart undercuts you on Wireless Headphones X200: €119.00 vs your €128.99 (was €133.52)."
    tripod = conn.execute("select p.price from products p where pid = 'tripod-flex' and competitor_id = 2").fetchone()[0]
    assert tripod == 5.0
    # unchanged prices on a re-crawl add no history and no alerts
    n = conn.execute("select count(*) from prices").fetchone()[0]
    run_all(conn, shop, None)
    assert conn.execute("select count(*) from prices").fetchone()[0] == n and len(alerts(conn)) == 4


def test_price_drop_alert_when_still_above_us(env):
    shop, conn, _ = env
    run_all(conn, shop, None)
    setp(shop, "gadgethub", "smartwatch-active-2", price=300.00)
    run_all(conn, shop, None)
    setp(shop, "gadgethub", "smartwatch-active-2", price=264.00)  # -12%, but ours (198.99) is still lower
    run_all(conn, shop, None)
    a = conn.execute("select kind, message from alerts").fetchall()
    assert [r["kind"] for r in a] == ["price_drop"] and "cut Smartwatch Active 2 by 12%" in a[0]["message"]


def test_layout_change_is_suspicious_not_a_mass_delisting(env):
    shop, conn, _ = env
    run_all(conn, shop, None)
    shop.post("/admin/layout", json={"broken": True}, headers={"x-token": TOKEN})
    for _ in range(2):
        res = run_all(conn, shop, None)
    assert {r["status"] for r in res} == {"suspicious"}
    assert [k for k, _, _ in alerts(conn)] == ["scraper_check", "scraper_check"]  # once per competitor, not per run
    assert conn.execute("select count(*) from products where active = 0").fetchone()[0] == 0
    shop.post("/admin/layout", json={"broken": False}, headers={"x-token": TOKEN})
    assert {r["status"] for r in run_all(conn, shop, None)} == {"ok"}
    assert len(alerts(conn)) == 2


def test_unreachable_site_alerts_once(env, monkeypatch):
    _, conn, _ = env
    monkeypatch.setattr("price_monitor.core.time.sleep", lambda s: None)  # skip retry backoff

    def down(request):
        raise httpx.ConnectError("refused", request=request)
    client = httpx.Client(transport=httpx.MockTransport(down))
    for _ in range(3):
        res = run_all(conn, client, None)
    assert {r["status"] for r in res} == {"failed"} and [k for k, _, _ in alerts(conn)] == ["crawl_failed"] * 2


def test_alerts_go_to_slack_once_and_wait_out_an_outage(env):
    shop, conn, _ = env
    run_all(conn, shop, None)
    setp(shop, "voltmart", HEADPHONES, price=119.00)
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        return httpx.Response(503)
    broken = Notifier(httpx.Client(transport=httpx.MockTransport(flaky)), slack_webhook="http://hooks/slack", sleep=lambda s: None)
    run_all(conn, shop, broken)
    assert calls["n"] == 4 and conn.execute("select notified from alerts").fetchone()[0] == 0  # kept for retry
    ok = Notifier(shop, slack_webhook="http://shop:8000/hooks/slack", sleep=lambda s: None)
    assert notify_pending(conn, ok) == 1 and notify_pending(conn, ok) == 0
    assert shop.get("/hooks/slack").json() == [{"text": conn.execute("select message from alerts").fetchone()[0]}]


def test_digest_lists_undercuts_and_crawl_health(env):
    shop, conn, _ = env
    run_all(conn, shop, None)
    setp(shop, "voltmart", HEADPHONES, price=119.00)
    run_all(conn, shop, None)
    subject, body = build_digest(conn)
    assert subject == "Price Watch weekly: 1 product(s) undercut"
    assert "Wireless Headphones X200" in body and "€119.00" in body and "7.7% above" in body and "GadgetHub: ok" in body
    out = []
    msg = send_digest(conn, "team@example.com", send=out.append)
    assert out == [msg] and msg["To"] == "team@example.com" and msg.get_body(("html",)) is not None


# --- panel ---

@pytest.fixture
def web(env, monkeypatch):
    shop, conn, path = env
    monkeypatch.setenv("DEMO_SEED", "true")
    run_all(conn, shop, None)
    setp(shop, "voltmart", HEADPHONES, price=119.00)
    run_all(conn, shop, None)
    return TestClient(create_app(path, secret="s"), follow_redirects=False), conn


def csrf(client, url):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(url).text).group(1)


def login(client):
    r = client.post("/login", data={"email": "pricing@example.com", "password": "password", "csrf": csrf(client, "/login")})
    assert r.status_code == 303


def test_panel_requires_login(web):
    client, _ = web
    assert client.get("/").headers["location"] == "/login"
    r = client.post("/login", data={"email": "pricing@example.com", "password": "x", "csrf": csrf(client, "/login")})
    assert "Wrong email or password" in r.text


def test_dashboard_product_chart_and_run_now(web):
    client, conn = web
    login(client)
    page = client.get("/").text
    assert page.count('data-pos="undercut"') == 1 and page.count('data-pos="cheapest"') == 11
    assert "VoltMart undercuts you on Wireless Headphones X200" in page
    product = client.get("/products/SKU-1001").text
    assert product.count("<path d=\"M") == 2 and "yours €128.99" in product
    assert client.post("/request/crawl", data={"csrf": "forged"}).status_code == 400
    r = client.post("/request/crawl", data={"csrf": csrf(client, "/")})
    assert r.status_code == 303 and conn.execute("select kind from requests").fetchone()[0] == "crawl"
    assert "GadgetHub" in client.get("/runs").text and "undercut" in client.get("/alerts").text
