import base64, hashlib, hmac, json, re, threading

import pytest
from fastapi.testclient import TestClient

from stock_sync import db as store
from stock_sync.channels import Amazon, Shopify, Warehouse
from stock_sync.fakes import create_app as create_fakes
from stock_sync.ledger import available, cancel_order, place_order, receive, retry_order, ship_order, targets
from stock_sync.sync import handle_wms, poll_amazon, push, reconcile
from stock_sync.web import create_app as create_web

ENV = {"SECRET_KEY": "s", "SHOPIFY_TOKEN": "shp", "AMAZON_TOKEN": "amz", "WMS_TOKEN": "wms", "FAKES_ADMIN_TOKEN": "admin",
       "SHOPIFY_WEBHOOK_SECRET": "shook", "WMS_WEBHOOK_SECRET": "whook", "HUB_URL": "http://hub", "SHOPIFY_URL": "http://apis/shopify",
       "AMAZON_URL": "http://apis/amazon", "WMS_URL": "http://apis/wms", "DEMO_SEED": "true"}
A = {"x-admin": "admin"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "s.db"))
    conn = store.connect(str(tmp_path / "s.db"))
    store.seed_catalog(conn)
    apis = TestClient(create_fakes(), base_url="http://apis")
    clients = {"shopify": Shopify(http=apis), "amazon": Amazon(http=apis)}
    yield conn, apis, clients
    conn.close()


def stock(conn, sku):
    r = conn.execute("select on_hand, reserved from skus where sku = ?", (sku,)).fetchone()
    return r["on_hand"], r["reserved"]


def shown(apis):
    s = apis.get("/_admin/state", headers=A).json()
    return s["shopify"]["levels"], s["amazon"]["qty"]


def test_bundles_buffers_and_caps(env):
    conn, *_ = env
    assert available(conn, "SET-STARTER") == 6  # limited by the 6 hand grinders
    assert available(conn, "MUG-PAIR") == 25
    want = {(l["channel"], l["sku"]): want for l in conn.execute("select * from listings") for want in [targets(conn)[l["id"]]]}
    assert want[("shopify", "BEAN-1KG")] == 18 and want[("amazon", "BEAN-1KG")] == 16  # Amazon keeps a buffer of 2
    assert want[("shopify", "FILTER-100")] == 50  # capped
    assert place_order(conn, "shopify", "1", [("SET-STARTER", 2), ("GRIND-H", 1)]) == "reserved"
    assert stock(conn, "GRIND-H") == (6, 3) and stock(conn, "BEAN-1KG") == (18, 2) and available(conn, "SET-STARTER") == 3


def test_orders_are_all_or_nothing_and_idempotent(env):
    conn, *_ = env
    assert place_order(conn, "shopify", "1", [("KETTLE-G", 2), ("MUG-BLK", 1)]) == "reserved"
    assert place_order(conn, "shopify", "1", [("KETTLE-G", 2), ("MUG-BLK", 1)]) == "reserved"  # webhook delivered twice
    assert stock(conn, "KETTLE-G") == (3, 2)
    assert place_order(conn, "amazon", "A2", [("MUG-BLK", 1), ("KETTLE-G", 2)]) == "oversold"
    assert stock(conn, "MUG-BLK") == (40, 1)  # nothing half-reserved
    exc = conn.execute("select detail from exceptions").fetchone()[0]
    assert "KETTLE-G: needs 2, 1 available" in exc
    oid = conn.execute("select id from orders where external_id = 'A2'").fetchone()[0]
    assert retry_order(conn, oid, "test") == "oversold"
    receive(conn, "KETTLE-G", 5, "GR-1")
    assert retry_order(conn, oid, "test") == "reserved" and stock(conn, "KETTLE-G") == (8, 4)
    assert conn.execute("select status from exceptions").fetchone()[0] == "resolved"
    assert place_order(conn, "shopify", "9", [("NOPE", 1)]) == "oversold"  # unknown SKU is an exception, not a crash


def test_last_units_sold_on_two_channels_at_once(env, tmp_path):
    conn, *_ = env
    results = []

    def buy(i):
        c = store.connect(str(tmp_path / "s.db"))
        try:
            results.append(place_order(c, "shopify" if i % 2 else "amazon", f"race-{i}", [("KETTLE-G", 1)]))
        finally:
            c.close()
    threads = [threading.Thread(target=buy, args=(i,)) for i in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count("reserved") == 3 and results.count("oversold") == 7
    assert stock(conn, "KETTLE-G") == (3, 3)


def test_ship_cancel_and_duplicate_receipts(env):
    conn, *_ = env
    place_order(conn, "shopify", "1", [("MUG-PAIR", 2)])
    assert stock(conn, "MUG-WHT") == (25, 2)
    assert handle_wms(conn, {"type": "shipment", "shipment_id": "S1", "order_ref": "shopify:1"}) == "shipped"
    assert stock(conn, "MUG-WHT") == (23, 0) and stock(conn, "MUG-BLK") == (38, 0)
    assert ship_order(conn, "shopify", "1") == "shipped"  # again: nothing changes
    assert stock(conn, "MUG-BLK") == (38, 0)
    place_order(conn, "shopify", "2", [("BEAN-1KG", 3)])
    assert cancel_order(conn, "shopify", "2") == "cancelled" and stock(conn, "BEAN-1KG") == (18, 0)
    assert handle_wms(conn, {"type": "receipt", "receipt_id": "GR-7", "sku": "BEAN-1KG", "qty": 10}) == "booked"
    assert handle_wms(conn, {"type": "receipt", "receipt_id": "GR-7", "sku": "BEAN-1KG", "qty": 10}) == "duplicate"
    assert stock(conn, "BEAN-1KG") == (28, 0)


def test_push_only_changes_and_survives_throttling(env):
    conn, apis, clients = env
    apis.post("/_admin/amazon/throttle", json={"count": 1}, headers=A)
    r = push(conn, clients)
    assert r["waits"] == {"amazon": 1.0} and r["pushed"] == 8  # Shopify done, Amazon paused
    r = push(conn, clients)
    assert r["pushed"] == 5
    shop, amz = shown(apis)
    assert shop["BEAN-1KG"] == 18 and amz["AMZ-BEAN-1KG"] == 16 and shop["SET-STARTER"] == 6
    writes = apis.get("/_admin/state", headers=A).json()
    place_order(conn, "shopify", "1", [("GRIND-H", 5)])
    r = push(conn, clients)
    # the grinder and the starter set (which contains it) change, on both channels; nothing else is re-sent
    assert r["pushed"] == 4
    shop, amz = shown(apis)
    assert (shop["GRIND-H"], amz["AMZ-GRIND-H"], shop["SET-STARTER"], amz["AMZ-SET-STARTER"]) == (1, 0, 1, 0)
    assert push(conn, clients)["pushed"] == 0


def test_amazon_orders_are_polled_with_pagination_and_cancellations(env):
    conn, apis, clients = env
    push(conn, clients)
    ids = [apis.post("/_admin/amazon/order", json={"lines": [{"sku": "AMZ-MUG-BLK", "qty": 1}]}, headers=A).json()["AmazonOrderId"] for _ in range(3)]
    assert poll_amazon(conn, clients["amazon"]) == 3 and poll_amazon(conn, clients["amazon"]) == 0
    assert stock(conn, "MUG-BLK") == (40, 3)
    apis.post("/_admin/amazon/cancel", json={"id": ids[0]}, headers=A)
    poll_amazon(conn, clients["amazon"])
    assert stock(conn, "MUG-BLK") == (40, 2)


def test_reconcile_books_a_count_difference_seen_twice_and_fixes_drift(env):
    conn, apis, clients = env
    push(conn, clients)
    wms = Warehouse(http=apis)
    apis.post("/_admin/wms/count", json={"sku": "MUG-WHT", "on_hand": 22}, headers=A)
    apis.post("/_admin/shopify/set", json={"ref": "inv_1004", "qty": 99}, headers=A)  # a manual edit in Shopify
    first = reconcile(conn, wms, clients)
    row = next(r for r in first["stock"] if r["sku"] == "MUG-WHT")
    assert row["action"] == "watching" and stock(conn, "MUG-WHT") == (25, 0)
    assert first["drift"] == [{"channel": "shopify", "sku": "GRIND-H", "pushed": 6, "shown": 99}]
    push(conn, clients)
    assert shown(apis)[0]["GRIND-H"] == 6
    second = reconcile(conn, wms, clients)
    assert next(r for r in second["stock"] if r["sku"] == "MUG-WHT")["action"] == "adjusted" and stock(conn, "MUG-WHT") == (22, 0)
    assert not second["drift"]


def test_webhooks_and_panel(env):
    conn, apis, clients = env
    web = TestClient(create_web(secret="s", clients=clients, wms=Warehouse(http=apis)), follow_redirects=False)
    order = {"id": 77, "name": "#1077", "line_items": [{"sku": "BEAN-1KG", "quantity": 2}]}
    body = json.dumps(order).encode()
    sig = base64.b64encode(hmac.new(b"shook", body, hashlib.sha256).digest()).decode()
    h = {"X-Shopify-Topic": "orders/create", "X-Shopify-Webhook-Id": "w1", "Content-Type": "application/json"}
    assert web.post("/webhooks/shopify", content=body, headers=h | {"X-Shopify-Hmac-Sha256": "forged"}).status_code == 401
    assert web.post("/webhooks/shopify", content=body, headers=h | {"X-Shopify-Hmac-Sha256": sig}).json()["result"] == "reserved"
    assert web.post("/webhooks/shopify", content=body, headers=h | {"X-Shopify-Hmac-Sha256": sig}).json() == {"duplicate": True}
    assert stock(conn, "BEAN-1KG") == (18, 2)
    ev = json.dumps({"type": "receipt", "receipt_id": "GR-1", "sku": "KETTLE-G", "qty": 4}).encode()
    assert web.post("/webhooks/wms", content=ev, headers={"X-WMS-Signature": "x"}).status_code == 401
    assert web.post("/webhooks/wms", content=ev, headers={"X-WMS-Signature": hmac.new(b"whook", ev, hashlib.sha256).hexdigest()}).json()["result"] == "booked"
    token = re.search(r'name="csrf" value="([^"]+)"', web.get("/login").text).group(1)
    web.post("/login", data={"email": "ops@example.com", "password": "password", "csrf": token})
    page = web.get("/").text
    token = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    assert 'data-sku="SET-STARTER" data-available="6"' in page and "limited by GRIND-H" in page
    assert "Say why" in web.post("/sku/MUG-BLK/adjust", data={"csrf": token, "delta": "-2", "reason": " "}).headers["location"].replace("%20", " ")
    assert "Adjusted" in web.post("/sku/MUG-BLK/adjust", data={"csrf": token, "delta": "-2", "reason": "broken"}).headers["location"]
    assert stock(conn, "MUG-BLK") == (38, 0)
    assert web.post("/reconcile", data={"csrf": token}).headers["location"] == "/reconcile"
    for p in ("/orders", "/exceptions", "/log", "/reconcile", "/sku/SET-STARTER", "/sku/MUG-BLK"):
        assert web.get(p).status_code == 200, p
