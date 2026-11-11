import base64, hashlib, hmac, json, re

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from shipflow import db as store, flow
from shipflow.fakes import create_app as create_fakes
from shipflow.rules import check_address, decide
from shipflow.web import CARRIERS, create_app as create_web

ENV = {"SECRET_KEY": "s", "CARRIER_KEY": "ck", "CARRIER_SECRET": "cs", "FAKES_ADMIN_TOKEN": "admin", "SHOP_TOKEN": "st", "SHOP_WEBHOOK_SECRET": "sh",
       "CARRIER_WEBHOOK_URL": "http://unreachable.invalid/c", "SHOP_WEBHOOK_URL": "http://unreachable.invalid/s", "CARRIER_URL": "http://apis/carrier",
       "SHOP_URL": "http://apis/shop", "FAKES_PUBLIC_URL": "http://apis", "DEMO_SEED": "true"}
DE = {"first_name": "Lena", "last_name": "Hoffmann", "address1": "Holtenauer Str. 12", "city": "Kiel", "zip": "24105", "country_code": "DE"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    conn = store.connect(str(tmp_path / "s.db"))
    apis = TestClient(create_fakes(), base_url="http://apis")
    yield conn, apis, flow.Carrier(http=apis), flow.Shop(http=apis), tmp_path
    conn.close()


def order(oid, address, lines, shipping="standard"):
    return {"id": oid, "name": f"#{oid}", "email": "c@mail.example", "total_price": "99.00", "shipping_address": address,
            "line_items": [{"sku": s, "quantity": q} for s, q in lines], "shipping_lines": [{"code": shipping}]}


def status(conn, oid):
    return conn.execute("select status, reason, carrier, service from orders where id = ?", (str(oid),)).fetchone()


def test_carrier_rules(env):
    conn, *_ = env
    cases = [(1, DE, [("EB-PRO", 1)], "standard", ("dhl", "Warenpost")),
             (2, DE, [("KB-K87", 2)], "standard", ("dhl", "Paket")),
             (3, {**DE, "address1": "Packstation 145", "address2": "Postnummer 12345678"}, [("HP-X200", 1)], "standard", ("dhl", "Paket to Packstation")),
             (4, {**DE, "country_code": "NL", "zip": "1012 AB", "city": "Amsterdam"}, [("HP-X200", 1)], "standard", ("dpd", "Classic EU")),
             (5, DE, [("HP-X200", 1)], "express", ("dhl_express", "Express next day")),
             (6, {**DE, "country_code": "CH", "zip": "8001", "city": "Zürich", "phone": "+41 44 123 45 67"}, [("SSD-1T", 1)], "standard", ("dhl_express", "Worldwide (with customs CN23)"))]
    for oid, a, lines, ship, (carrier, service) in cases:
        assert flow.receive(conn, order(oid, a, lines, ship)) == "ready", oid
        assert tuple(status(conn, oid))[2:] == (carrier, service), oid
    assert flow.receive(conn, order(1, DE, [("EB-PRO", 1)])) == "duplicate"


def test_holds_explain_what_to_fix(env):
    conn, *_ = env
    holds = {10: ({**DE, "address1": "Holtenauer Strasse"}, [("EB-PRO", 1)], "no house number"),
             11: ({**DE, "zip": "2410"}, [("EB-PRO", 1)], "isn't a valid DE postcode"),
             12: ({**DE, "country_code": "CH", "zip": "8001"}, [("EB-PRO", 1)], "phone number is needed"),
             13: ({**DE, "country_code": "US", "zip": "10001", "phone": "+1 212"}, [("DESK-L", 1)], "over the 31.5 kg"),
             14: ({**DE, "country_code": "GB", "zip": "SW1A 1AA", "phone": "+44"}, [("DESK-L", 0), ("EB-PRO", 1), ("XX-1", 1)], "unknown SKU")}
    for oid, (a, lines, why) in holds.items():
        assert flow.receive(conn, order(oid, a, lines)) == "held"
        assert why in status(conn, oid)["reason"], oid
    assert "customs: no HS code for DESK-L" in decide({"address": {"name": "A", "street": "Main St 1", "city": "Bern", "postcode": "3000", "country": "CH", "phone": "1"},
                                                       "lines": [{"sku": "DESK-L", "qty": 0}]}, flow.products(conn))["hold"]
    assert flow.fix(conn, "10", {"street": "Holtenauer Str. 12"}, "Wim") == "ready"


def test_labels_once_customs_and_refusals(env):
    conn, apis, carrier, shop, tmp = env
    flow.receive(conn, order(20, DE, [("HP-X200", 1)]))
    flow.receive(conn, order(21, {**DE, "country_code": "CH", "zip": "8001", "city": "Zürich", "phone": "+41 44"}, [("SSD-1T", 2)]))
    flow.receive(conn, order(22, {**DE, "city": "Nowhere"}, [("EB-PRO", 1)]))
    assert flow.label(conn, carrier, "20", tmp / "labels") == "labelled"
    assert flow.label(conn, carrier, "21", tmp / "labels") == "labelled"
    assert flow.label(conn, carrier, "22", tmp / "labels") == "held" and "could not be found by the carrier" in status(conn, 22)["reason"]
    parcels = apis.get("/_admin/state", headers={"x-admin": "admin"}).json()["parcels"]
    ch = next(p for p in parcels if p["order_number"] == "#21")
    assert ch["parcel_items"][0]["hs_code"] == "847170" and ch["parcel_items"][0]["origin_country"] == "KR"
    # a label made but the answer lost: the retry finds it instead of buying another
    conn.execute("delete from parcels where order_id = '20'")
    conn.execute("update orders set status = 'ready' where id = '20'")
    assert flow.label(conn, carrier, "20", tmp / "labels") == "labelled"
    assert len([p for p in apis.get("/_admin/state", headers={"x-admin": "admin"}).json()["parcels"] if p["order_number"] == "#20"]) == 1


def test_batch_fulfil_track_and_stuck(env):
    conn, apis, carrier, shop, tmp = env
    for oid in (30, 31, 32):
        flow.receive(conn, order(oid, DE, [("EB-PRO", 1)]))
        flow.label(conn, carrier, str(oid), tmp / "labels")
    assert flow.fulfil(conn, shop, CARRIERS) == 0  # not printed yet: not handed to the carrier
    b = flow.batch(conn, "Wim", tmp / "batches")
    assert b["parcels"] == 3 and len(PdfReader(b["path"]).pages) == 3 and flow.batch(conn, "Wim", tmp / "batches") is None
    assert flow.fulfil(conn, shop, CARRIERS) == 3
    f = apis.get("/_admin/state", headers={"x-admin": "admin"}).json()["fulfillments"]
    assert f[0]["tracking_company"] == "DHL" and f[0]["notify_customer"] is True
    pid = lambda oid: conn.execute("select carrier_parcel_id from parcels where order_id = ?", (str(oid),)).fetchone()[0]
    assert flow.track(conn, {"id": "e1", "parcel_id": pid(30), "status": "delivered"}) == "delivered"
    assert flow.track(conn, {"id": "e1", "parcel_id": pid(30), "status": "delivered"}) == "duplicate"
    flow.track(conn, {"id": "e2", "parcel_id": pid(31), "status": "address_unknown", "message": "recipient not known at address"})
    assert status(conn, 31)["status"] == "exception" and "can't find the address" in status(conn, 31)["reason"]
    conn.execute("update parcels set created_at = '2020-01-01 00:00:00' where order_id = '32'")
    assert flow.stuck(conn) == ["32"] and "no scan" in status(conn, 32)["reason"]


def test_webhooks_and_panel(env):
    conn, apis, carrier, shop, tmp = env
    web = TestClient(create_web(secret="s", carrier=carrier), follow_redirects=False)
    body = json.dumps(order(40, DE, [("EB-PRO", 1)])).encode()
    sig = base64.b64encode(hmac.new(b"sh", body, hashlib.sha256).digest()).decode()
    assert web.post("/webhooks/shop", content=body, headers={"X-Shopify-Hmac-Sha256": "x"}).status_code == 401
    assert web.post("/webhooks/shop", content=body, headers={"X-Shopify-Hmac-Sha256": sig, "X-Shopify-Webhook-Id": "w1"}).json()["status"] == "ready"
    assert web.post("/webhooks/shop", content=body, headers={"X-Shopify-Hmac-Sha256": sig, "X-Shopify-Webhook-Id": "w1"}).json() == {"duplicate": True}
    token = re.search(r'name="csrf" value="([^"]+)"', web.get("/login").text).group(1)
    web.post("/login", data={"email": "warehouse@example.com", "password": "password", "csrf": token})
    token = re.search(r'name="csrf" value="([^"]+)"', web.get("/").text).group(1)
    assert "1+label" in web.post("/labels", data={"csrf": token}).headers["location"].replace("%20", "+")
    loc = web.post("/batches", data={"csrf": token}).headers["location"]
    bid = re.search(r"batch=(\d+)", loc).group(1)
    assert web.get(f"/batches/{bid}.pdf").headers["content-type"] == "application/pdf"
    ev = json.dumps({"action": "parcel_status_changed", "id": "x1", "parcel": {"id": int(conn.execute("select carrier_parcel_id from parcels").fetchone()[0]),
                                                                               "status": {"message": "in_transit"}}}).encode()
    assert web.post("/webhooks/carrier", content=ev, headers={"Sendcloud-Signature": "00"}).status_code == 401
    assert web.post("/webhooks/carrier", content=ev, headers={"Sendcloud-Signature": hmac.new(b"cs", ev, hashlib.sha256).hexdigest()}).json()["status"] == "in_transit"
    for p in ("/?status=held", "/?status=labelled", "/rules", "/orders/40"):
        assert web.get(p).status_code == 200


def test_address_checks():
    assert check_address({"name": "A", "street": "Packstation 145", "city": "Kiel", "postcode": "24105", "country": "DE"}) == []
    assert "postcode" in check_address({"name": "A", "street": "1 Main St", "city": "London", "postcode": "12345", "country": "GB", "phone": "1"})[0]
