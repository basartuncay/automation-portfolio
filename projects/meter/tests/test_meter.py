import hashlib, hmac, json, re, time

import pytest
from fastapi.testclient import TestClient

from meter import billing, db as store
from meter.fakes import create_app as create_fakes
from meter.stripe_api import Stripe, verify
from meter.web import create_app as create_web

ENV = {"SECRET_KEY": "s", "STRIPE_SECRET_KEY": "sk_test_x", "STRIPE_WEBHOOK_SECRET": "whsec_x", "STRIPE_URL": "http://stripe",
       "STRIPE_WEBHOOK_URL": "http://unreachable.invalid/webhooks/stripe", "FAKES_ADMIN_TOKEN": "admin", "DEMO_SEED": "true"}
A = {"x-admin": "admin"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "m.db"))
    conn = store.connect(str(tmp_path / "m.db"))
    fake = TestClient(create_fakes(), base_url="http://stripe")
    yield conn, fake, Stripe(http=fake)
    conn.close()


def cust(conn, cid):
    return conn.execute("select * from customers where id = ?", (cid,)).fetchone()


def new(conn, stripe, plan="starter", cid="c1"):
    key = billing.create_customer(conn, stripe, cid=cid, name="Acme", email="billing@acme.example", plan_id=plan)
    return key, cust(conn, cid)


def ev(n, key, ts=None):
    return {"metric": "api_calls", "quantity": n, "idempotency_key": key, **({"timestamp": ts} if ts else {})}


def test_events_are_counted_once_and_validated(env):
    conn, fake, stripe = env
    key, c = new(conn, stripe)
    assert key.startswith("mk_live_") and c["api_key_hash"] != key  # only the hash is stored
    r = billing.ingest(conn, c, [ev(500, "a"), ev(500, "a"), ev(0, "b"), {"metric": "x", "quantity": 1, "idempotency_key": "c"},
                                 ev(5, "d", time.time() + 3600), ev(5, "e", time.time() - 40 * 86400)])
    assert (r["accepted"], r["duplicates"], r["used"]) == (1, 1, 500)
    assert [x["error"][:12] for x in r["rejected"]] == ["quantity mus", "metric must ", "timestamp is", "older than 3"]


def test_projection_alerts_and_cap(env):
    conn, fake, stripe = env
    _, c = new(conn, stripe)
    billing.ingest(conn, c, [ev(8_000, "a")])
    assert [r["subject"] for r in conn.execute("select subject from outbox")] == ["You've used 80% of the 10,000 API calls in your plan"]
    billing.ingest(conn, c, [ev(3_500, "b")])  # 11,500 used: 1,500 over = 2 started thousands
    plan = conn.execute("select * from plans where id = 'starter'").fetchone()
    assert billing.projected_cents(plan, 11_500) == 2900 + 2 * 200
    assert conn.execute("select count(*) from outbox").fetchone()[0] == 2
    billing.ingest(conn, c, [ev(1, "c")])
    assert conn.execute("select count(*) from outbox").fetchone()[0] == 2  # each alert once per period
    conn.execute("update customers set spend_cap_cents = 3500 where id = 'c1'")
    assert billing.entitlement(conn, cust(conn, "c1"))["allowed"]  # 11,501 calls: €33 so far, under the €35 cap
    billing.ingest(conn, cust(conn, "c1"), [ev(1_000, "d")])  # 12,501: 3 started thousands over, €35
    e = billing.entitlement(conn, cust(conn, "c1"))
    assert not e["allowed"] and "spend cap" in e["reason"]
    assert "Spend cap reached" in [r[0] for r in conn.execute("select subject from outbox")]


def test_reporting_is_idempotent_and_reconciles(env):
    conn, fake, stripe = env
    _, c = new(conn, stripe)
    billing.ingest(conn, c, [ev(700, "a"), ev(300, "b")])
    assert billing.report(conn, stripe) == 2
    conn.execute("update events set reported = 0")  # as if we crashed after sending, before marking
    assert billing.report(conn, stripe) == 2
    assert fake.get("/_admin/state", headers=A).json()["metered"][c["stripe_customer"]] == 1000  # Stripe dropped the repeats
    assert billing.reconcile(conn, stripe) == [{"customer": "c1", "name": "Acme", "ours": 1000, "stripe": 1000, "ok": True}]


def signed(ev_):
    body = json.dumps(ev_).encode()
    t = int(time.time())
    return body, f"t={t},v1={hmac.new(b'whsec_x', f'{t}.'.encode() + body, hashlib.sha256).hexdigest()}"


def deliver_all(conn, stripe, fake_events):
    for e in fake_events:
        billing.handle_event(conn, stripe, e)


def test_period_close_invoice_failed_payment_grace_and_restore(env):
    conn, fake, stripe = env
    _, c = new(conn, stripe)
    billing.ingest(conn, c, [ev(12_000, "a")])
    billing.report(conn, stripe)
    fake.post("/_admin/card", json={"customer": c["stripe_customer"], "ok": False}, headers=A)
    inv = fake.post("/_admin/close_period", json={"customer": c["stripe_customer"]}, headers=A).json()
    assert inv["amount_due"] == 2900 + 2 * 200
    sub = fake.get("/_admin/state", headers=A).json()["subscriptions"][c["stripe_subscription"]]
    mk = lambda t, o: {"id": "evt_" + t + str(time.time_ns()), "type": t, "data": {"object": o}}
    deliver_all(conn, stripe, [mk("invoice.finalized", {**inv, "status": "open"}), mk("invoice.payment_failed", {**inv, "status": "open", "attempt_count": 1}),
                               mk("customer.subscription.updated", sub)])
    c = cust(conn, "c1")
    assert c["status"] == "past_due" and c["period_start"] != billing.iso(inv["period_start"])
    assert billing.entitlement(conn, c)["allowed"]  # grace: still working while the card is retried
    assert billing.enforce_grace(conn, days=0) == ["c1"]
    assert not billing.entitlement(conn, cust(conn, "c1"))["allowed"]
    paid = mk("invoice.paid", {**inv, "status": "paid", "amount_paid": inv["amount_due"], "attempt_count": 2})
    billing.handle_event(conn, stripe, paid)
    assert cust(conn, "c1")["status"] == "active" and billing.handle_event(conn, stripe, paid) == "duplicate"
    assert billing.usage(conn, cust(conn, "c1")) == 0  # new period, fresh count


def test_late_usage_is_billed_in_the_current_period(env):
    conn, fake, stripe = env
    _, c = new(conn, stripe)
    conn.execute("update customers set period_start = ? where id = 'c1'", (billing.iso(time.time() - 3600),))
    r = billing.ingest(conn, cust(conn, "c1"), [ev(50, "late", time.time() - 2 * 86400)])
    assert r["accepted"] == 1 and conn.execute("select late from events").fetchone()[0] == 1


def test_upgrade_now_downgrade_at_period_end(env):
    conn, fake, stripe = env
    _, c = new(conn, stripe, plan="pro")
    assert "when this period ends" in billing.change_plan(conn, stripe, "c1", "starter", "Bo")
    assert cust(conn, "c1")["plan_id"] == "pro" and cust(conn, "c1")["pending_plan_id"] == "starter"
    assert "Upgraded to Scale now" in billing.change_plan(conn, stripe, "c1", "scale", "Bo")
    assert cust(conn, "c1")["plan_id"] == "scale" and cust(conn, "c1")["pending_plan_id"] is None
    assert fake.get("/_admin/state", headers=A).json()["subscriptions"][c["stripe_subscription"]]["items"][0] == "price_scale_base"
    billing.change_plan(conn, stripe, "c1", "starter", "Bo")
    sub = fake.get("/_admin/state", headers=A).json()["subscriptions"][c["stripe_subscription"]]
    sub = {**sub, "current_period_start": sub["current_period_end"], "current_period_end": sub["current_period_end"] + 30 * 86400}
    billing.handle_event(conn, stripe, {"id": "evt_p", "type": "customer.subscription.updated", "data": {"object": sub}})
    assert cust(conn, "c1")["plan_id"] == "starter"


def test_api_webhooks_and_panel(env):
    conn, fake, stripe = env
    web = TestClient(create_web(secret="s", stripe=stripe), follow_redirects=False)
    token = re.search(r'name="csrf" value="([^"]+)"', web.get("/login").text).group(1)
    web.post("/login", data={"email": "billing@example.com", "password": "password", "csrf": token})
    token = re.search(r'name="csrf" value="([^"]+)"', web.get("/").text).group(1)
    web.post("/customers", data={"csrf": token, "name": "Nordlicht", "email": "b@n.example", "plan": "starter"})
    key = re.search(r'id="new-key">([^<]+)<', web.get("/").text).group(1)
    assert "id=\"new-key\"" not in web.get("/").text  # shown once
    h = {"Authorization": f"Bearer {key}"}
    assert web.post("/v1/events", json={"events": [ev(10, "x")]}, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert web.post("/v1/events", json={"events": [ev(10, "x"), ev(10, "x")]}, headers=h).json()["accepted"] == 1
    assert web.get("/v1/entitlement", headers=h).json()["remaining_included"] == 9_990
    body, sig = signed({"id": "evt_z", "type": "invoice.paid", "data": {"object": {"customer": "cus_nope"}}})
    assert web.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": "t=1,v1=00"}).status_code == 400
    assert web.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": sig}).json()["result"] == "unknown customer"
    assert not verify(body, f"t={int(time.time()) - 3600},v1=00", "whsec_x")
    for p in ("/", "/reconcile", "/emails"):
        assert web.get(p).status_code == 200
    cid = conn.execute("select id from customers").fetchone()[0]
    assert "API access: allowed" in web.get(f"/customers/{cid}").text
