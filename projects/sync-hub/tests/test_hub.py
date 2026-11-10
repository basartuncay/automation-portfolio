import base64, hashlib, hmac, json, re, time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from sync_hub import db as store, worker
from sync_hub.clients import CRM, Shop
from sync_hub.engine import from_crm, norm_phone, resolve
from sync_hub.fakes import create_app as create_fakes
from sync_hub.web import create_app as create_hub

ADMIN = {"x-admin": "a" * 32}
REDIRECT = "http://hub/oauth/crm/callback"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {"FAKES_ADMIN_TOKEN": "a" * 32, "CRM_CLIENT_SECRET": "c" * 32, "SHOP_ACCESS_TOKEN": "s" * 32,
                 "SHOP_WEBHOOK_SECRET": "w" * 32, "TOKEN_KEY": "k" * 32, "CRM_URL": "http://fakes/crm",
                 "SHOP_URL": "http://fakes/shop", "CRM_PUBLIC_URL": "http://fakes/crm", "PUBLIC_URL": "http://hub"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SHOP_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(worker, "BACKOFF", 0)
    monkeypatch.setattr(worker, "MAX_ATTEMPTS", 3)


@pytest.fixture
def hub(tmp_path):
    fakes = TestClient(create_fakes(), base_url="http://fakes")
    conn = store.connect(str(tmp_path / "hub.db"))
    crm, shop = CRM(conn, fakes), Shop(fakes)
    yield fakes, conn, crm, shop
    conn.close()


def connect_crm(fakes, crm):
    loc = fakes.post("/crm/oauth/authorize", data={"redirect_uri": REDIRECT, "state": "x"}, follow_redirects=False).headers["location"]
    crm.exchange(parse_qs(urlparse(loc).query)["code"][0], REDIRECT)


def signup(fakes, conn, **fields):
    c = fakes.post("/shop/_admin/customers", json=fields, headers=ADMIN).json()
    worker.enqueue(conn, "shop", f"shop:t{time.time_ns()}", f"shop:{c['id']}", "customers/create", c)
    return c


def shop_event(fakes, conn, cid):
    c = fakes.get(f"/shop/admin/api/2026-10/customers/{cid}.json", headers={"X-Shopify-Access-Token": "s" * 32}).json()["customer"]
    worker.enqueue(conn, "shop", f"shop:t{time.time_ns()}", f"shop:{cid}", "customers/update", c)


def crm_state(fakes):
    return fakes.get("/crm/_admin/state", headers=ADMIN).json()


def shop_state(fakes):
    return fakes.get("/shop/_admin/state", headers=ADMIN).json()


def sync(conn, crm, shop):
    worker.poll_crm(conn, crm)
    return worker.drain(conn, crm, shop)


def test_oauth_tokens_are_stored_encrypted(hub):
    fakes, conn, crm, _ = hub
    connect_crm(fakes, crm)
    row = conn.execute("select * from connections where name = 'crm'").fetchone()
    assert row["status"] == "connected" and row["access_token"].startswith("gAAAA")  # Fernet ciphertext, not the token
    assert len(store.unseal(row["access_token"])) > 20


def test_new_store_customer_appears_in_crm_and_the_echo_is_ignored(hub):
    fakes, conn, crm, shop = hub
    connect_crm(fakes, crm)
    signup(fakes, conn, email="Ana.Silva@Example.com", first_name="Ana", last_name="Silva", phone="+351 912 345 678")
    assert worker.drain(conn, crm, shop) == {"created": 1}
    contact = crm_state(fakes)["contacts"][0]["properties"]
    assert contact == {"email": "ana.silva@example.com", "firstname": "Ana", "lastname": "Silva", "phone": "+351912345678",
                       "total_spent": "0.00", "lifecyclestage": "lead"}
    writes = (crm_state(fakes)["stats"]["writes"], shop_state(fakes)["stats"]["writes"])
    assert sync(conn, crm, shop) == {"noop": 1}  # the poll sees our own write: recognised, nothing written back
    assert sync(conn, crm, shop) == {}
    assert (crm_state(fakes)["stats"]["writes"], shop_state(fakes)["stats"]["writes"]) == writes


def test_crm_edit_reaches_the_store(hub):
    fakes, conn, crm, shop = hub
    connect_crm(fakes, crm)
    c = signup(fakes, conn, email="tom@example.com", first_name="Tom", last_name="Berg")
    sync(conn, crm, shop), sync(conn, crm, shop)
    cid = int(crm_state(fakes)["contacts"][0]["id"])
    fakes.post(f"/crm/_admin/contacts/{cid}", json={"phone": "0049 30 1234567", "lastname": "Bergmann"}, headers=ADMIN)
    assert sync(conn, crm, shop) == {"updated": 1}
    s = shop_state(fakes)["customers"][0]
    assert (s["phone"], s["last_name"]) == ("+49301234567", "Bergmann")
    log = json.loads(conn.execute("select detail from log where action = 'updated'").fetchone()[0])
    assert log == {"last_name": ["Berg", "Bergmann"], "phone": [None, "+49301234567"]}


def test_one_way_fields_respect_their_owner(hub):
    fakes, conn, crm, shop = hub
    connect_crm(fakes, crm)
    c = signup(fakes, conn, email="mia@example.com", first_name="Mia")
    sync(conn, crm, shop), sync(conn, crm, shop)
    fakes.post("/shop/_admin/orders", json={"customer_id": c["id"], "amount": 129.5}, headers=ADMIN)
    shop_event(fakes, conn, c["id"])
    sync(conn, crm, shop)
    p = crm_state(fakes)["contacts"][0]["properties"]
    assert (p["total_spent"], p["lifecyclestage"]) == ("129.50", "customer")
    cid = int(crm_state(fakes)["contacts"][0]["id"])
    fakes.post(f"/crm/_admin/contacts/{cid}", json={"total_spent": "0", "vip": "true"}, headers=ADMIN)
    sync(conn, crm, shop)
    s = shop_state(fakes)["customers"][0]
    assert s["total_spent"] == "129.50" and s["tags"] == "VIP"  # money stays the store's; VIP comes from the CRM


def test_conflicting_edits_wait_for_a_person(hub):
    fakes, conn, crm, shop = hub
    connect_crm(fakes, crm)
    c = signup(fakes, conn, email="lee@example.com", first_name="Lee", phone="+44 20 1111 1111")
    sync(conn, crm, shop), sync(conn, crm, shop)
    cid = int(crm_state(fakes)["contacts"][0]["id"])
    fakes.post(f"/shop/_admin/customers/{c['id']}", json={"phone": "+44 20 2222 2222", "silent": True}, headers=ADMIN)
    fakes.post(f"/crm/_admin/contacts/{cid}", json={"phone": "+44 20 3333 3333"}, headers=ADMIN)
    assert sync(conn, crm, shop) == {"conflict": 1}
    assert shop_state(fakes)["customers"][0]["phone"] == "+44 20 2222 2222"  # neither side overwritten
    assert crm_state(fakes)["contacts"][0]["properties"]["phone"] == "+44 20 3333 3333"  # as the sales rep typed it
    conflict = conn.execute("select * from conflicts").fetchone()
    assert (conflict["field"], json.loads(conflict["shop_value"]), json.loads(conflict["crm_value"])) == ("phone", "+442022222222", "+442033333333")
    resolve(conn, crm, shop, conflict["id"], "shop", "Ola")
    assert crm_state(fakes)["contacts"][0]["properties"]["phone"] == "+442022222222"
    assert sync(conn, crm, shop) == {"noop": 1} and sync(conn, crm, shop) == {}  # settled, no loop


def test_outage_retries_then_dead_letter_then_replay(hub):
    fakes, conn, crm, shop = hub
    connect_crm(fakes, crm)
    fakes.post("/crm/_admin/fail", json={"mode": "503", "count": 99}, headers=ADMIN)
    signup(fakes, conn, email="zoe@example.com", first_name="Zoe")
    assert [worker.drain(conn, crm, shop) for _ in range(3)] == [{"retry": 1}, {"retry": 1}, {"dead": 1}]
    ev = conn.execute("select * from events").fetchone()
    assert ev["status"] == "dead" and "503" in ev["last_error"] and crm_state(fakes)["contacts"] == []
    fakes.post("/crm/_admin/fail", json={"mode": "ok"}, headers=ADMIN)
    conn.execute("update events set status = 'queued', attempts = 0, next_at = 0 where status = 'dead'")
    assert worker.drain(conn, crm, shop) == {"created": 1}


def test_rate_limit_waits_as_told(hub):
    fakes, conn, crm, shop = hub
    connect_crm(fakes, crm)
    fakes.post("/crm/_admin/fail", json={"mode": "429", "count": 1}, headers=ADMIN)
    signup(fakes, conn, email="rob@example.com")
    assert worker.drain(conn, crm, shop) == {"retry": 1}
    assert conn.execute("select next_at from events").fetchone()[0] >= time.time() + 0.5  # Retry-After: 1
    assert worker.drain(conn, crm, shop) == {}


def test_expired_token_is_refreshed_transparently(hub):
    fakes, conn, crm, shop = hub
    connect_crm(fakes, crm)
    fakes.post("/crm/_admin/expire_tokens", headers=ADMIN)
    signup(fakes, conn, email="ivy@example.com")
    assert worker.drain(conn, crm, shop) == {"created": 1}
    assert crm_state(fakes)["stats"]["refreshes"] == 1


def test_changes_to_one_customer_stay_in_order(hub):
    fakes, conn, crm, shop = hub
    connect_crm(fakes, crm)
    worker.enqueue(conn, "shop", "e1", "shop:1", "t", {"id": 1})
    worker.enqueue(conn, "shop", "e2", "shop:1", "t", {"id": 1})
    worker.enqueue(conn, "shop", "e3", "shop:2", "t", {"id": 2})
    conn.execute("update events set next_at = ? where external_id = 'e1'", (time.time() + 60,))  # e1 is backing off
    assert [e["external_id"] for e in worker.due_events(conn)] == ["e3"]
    assert not worker.enqueue(conn, "shop", "e3", "shop:2", "t", {"id": 2})  # duplicate delivery ignored


def test_record_without_email_is_a_permanent_failure(hub):
    fakes, conn, crm, shop = hub
    connect_crm(fakes, crm)
    signup(fakes, conn, first_name="NoMail")
    assert worker.drain(conn, crm, shop) == {"dead": 1}
    assert "no email" in conn.execute("select last_error from events").fetchone()[0]


def test_phone_normalisation():
    assert norm_phone("0049 (30) 123-45") == "+493012345" and norm_phone("+1 415 555 0100") == "+14155550100" and norm_phone("") is None
    assert from_crm({"properties": {"email": "A@B.C", "vip": "true", "total_spent": "3"}})["vip"] is True


# --- panel and webhooks ---

def sign(body: bytes) -> str:
    return base64.b64encode(hmac.new(b"w" * 32, body, hashlib.sha256).digest()).decode()


@pytest.fixture
def web(hub, monkeypatch):
    fakes, conn, crm, shop = hub
    monkeypatch.setenv("DEMO_SEED", "true")
    path = conn.execute("pragma database_list").fetchone()[2]
    client = TestClient(create_hub(path, secret="s", http=fakes), base_url="http://hub", follow_redirects=False)
    return client, fakes, conn, crm, shop


def csrf(client, url):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(url).text).group(1)


def test_webhook_signature_and_duplicates(web):
    client, _, conn, _, _ = web
    body = json.dumps({"id": 7001, "email": "x@y.z"}).encode()
    h = {"X-Shopify-Topic": "customers/update", "X-Shopify-Webhook-Id": "abc", "Content-Type": "application/json"}
    assert client.post("/webhooks/shop", content=body, headers=h | {"X-Shopify-Hmac-Sha256": "forged"}).status_code == 401
    assert client.post("/webhooks/shop", content=body, headers=h | {"X-Shopify-Hmac-Sha256": sign(body)}).json() == {"queued": True}
    assert client.post("/webhooks/shop", content=body, headers=h | {"X-Shopify-Hmac-Sha256": sign(body)}).json() == {"queued": False}
    assert conn.execute("select count(*) from events").fetchone()[0] == 1


def test_connect_crm_through_the_panel(web):
    client, fakes, conn, crm, shop = web
    assert client.get("/").headers["location"] == "/login"
    client.post("/login", data={"email": "ops@example.com", "password": "password", "csrf": csrf(client, "/login")})
    loc = client.post("/connect/crm", data={"csrf": csrf(client, "/")}).headers["location"]
    q = parse_qs(urlparse(loc).query)
    assert loc.startswith("http://fakes/crm/oauth/authorize") and q["client_id"] == ["sync-hub"]
    assert client.get("/oauth/crm/callback", params={"code": "x", "state": "wrong"}).status_code == 400
    loc = client.post("/connect/crm", data={"csrf": csrf(client, "/")}).headers["location"]
    state = parse_qs(urlparse(loc).query)["state"][0]
    back = fakes.post("/crm/oauth/authorize", data={"redirect_uri": "http://hub/oauth/crm/callback", "state": state}, follow_redirects=False).headers["location"]
    r = client.get(back.replace("http://hub", ""))
    assert "CRM+connected" in r.headers["location"] and crm.connected()
    for page in ("/", "/log", "/conflicts", "/dead", "/mapping"):
        assert client.get(page).status_code == 200
    assert "connected" in client.get("/").text


def test_concurrent_edits_on_both_sides_are_not_lost(hub):
    """Regression: a store change synced to the CRM must not swallow an unsynced CRM change in other fields."""
    fakes, conn, crm, shop = hub
    connect_crm(fakes, crm)
    c = signup(fakes, conn, email="ana@example.com", first_name="Ana", last_name="Silva")
    sync(conn, crm, shop), sync(conn, crm, shop)
    cid = int(crm_state(fakes)["contacts"][0]["id"])
    fakes.post(f"/crm/_admin/contacts/{cid}", json={"lastname": "Silva Costa", "vip": "true"}, headers=ADMIN)
    fakes.post("/shop/_admin/orders", json={"customer_id": c["id"], "amount": 89.9}, headers=ADMIN)
    shop_event(fakes, conn, c["id"])
    worker.drain(conn, crm, shop)  # the store event is handled before the CRM poll picks up the CRM edit
    sync(conn, crm, shop)
    s = shop_state(fakes)["customers"][0]
    assert (s["last_name"], s["tags"]) == ("Silva Costa", "VIP")
    assert crm_state(fakes)["contacts"][0]["properties"]["total_spent"] == "89.90"
