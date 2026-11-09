import hashlib, hmac, json, time

from fastapi.testclient import TestClient

from stripe_fulfillment import app as appmod
from stripe_fulfillment.app import Store, create_app

SECRET = "whsec_test_secret"


class Mailbox:
    def __init__(self):
        self.sent = []

    def send(self, to, subject, body):
        self.sent.append((to, subject, body))


def signed(payload: dict, secret=SECRET, ts=None):
    body = json.dumps(payload)
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    return body, {"Stripe-Signature": f"t={ts},v1={sig}", "Content-Type": "application/json"}


def event(eid, etype, obj):
    return {"id": eid, "object": "event", "type": etype, "data": {"object": obj}}


CHECKOUT = {"id": "cs_1", "object": "checkout.session", "payment_status": "paid", "customer": "cus_1",
            "customer_details": {"email": "buyer@example.com"}, "metadata": {"product": "pro"}}


def setup(tmp_path):
    store, box = Store(str(tmp_path / "db.sqlite")), Mailbox()
    return TestClient(create_app(SECRET, store, box)), store, box


def post(client, payload, **kw):
    body, headers = signed(payload, **kw)
    return client.post("/webhooks/stripe", content=body, headers=headers)


def test_paid_checkout_grants_license_and_emails(tmp_path):
    client, store, box = setup(tmp_path)
    r = post(client, event("evt_1", "checkout.session.completed", CHECKOUT)).json()
    key = r["result"].split()[-1]
    assert key.startswith("LIC-") and box.sent[0][0] == "buyer@example.com" and key in box.sent[0][2]
    assert client.get(f"/licenses/{key}").json() == {"valid": True, "status": "active", "product": "pro"}


def test_bad_or_old_signature_rejected(tmp_path):
    client, store, box = setup(tmp_path)
    assert post(client, event("evt_1", "checkout.session.completed", CHECKOUT), secret="whsec_wrong").status_code == 400
    assert post(client, event("evt_1", "checkout.session.completed", CHECKOUT), ts=int(time.time()) - 3600).status_code == 400
    assert box.sent == []


def test_retried_event_is_fulfilled_once(tmp_path):
    client, store, box = setup(tmp_path)
    ev = event("evt_1", "checkout.session.completed", CHECKOUT)
    post(client, ev)
    assert post(client, ev).json() == {"duplicate": True}
    assert len(box.sent) == 1 and store.db.execute("select count(*) from licenses").fetchone()[0] == 1


def test_second_purchase_event_does_not_duplicate_active_license(tmp_path):
    client, store, box = setup(tmp_path)
    post(client, event("evt_1", "checkout.session.completed", CHECKOUT))
    r = post(client, event("evt_2", "checkout.session.async_payment_succeeded", CHECKOUT)).json()
    assert r["result"].startswith("already active")


def test_unpaid_async_checkout_waits(tmp_path):
    client, store, box = setup(tmp_path)
    r = post(client, event("evt_1", "checkout.session.completed", {**CHECKOUT, "payment_status": "unpaid"})).json()
    assert r["result"] == "waiting for async payment" and box.sent == []


def test_failed_payment_then_paid_then_cancelled(tmp_path):
    client, store, box = setup(tmp_path)
    key = post(client, event("evt_1", "checkout.session.completed", CHECKOUT)).json()["result"].split()[-1]
    post(client, event("evt_2", "invoice.payment_failed", {"customer": "cus_1", "customer_email": "buyer@example.com",
                                                           "hosted_invoice_url": "https://pay.example/inv"}))
    assert client.get(f"/licenses/{key}").json()["status"] == "past_due" and "pay.example" in box.sent[-1][2]
    post(client, event("evt_3", "invoice.paid", {"customer": "cus_1"}))
    assert client.get(f"/licenses/{key}").json()["valid"] is True
    post(client, event("evt_4", "customer.subscription.deleted", {"customer": "cus_1"}))
    assert client.get(f"/licenses/{key}").json() == {"valid": False, "status": "revoked", "product": "pro"}


def test_failure_rolls_back_and_lets_stripe_retry(tmp_path, monkeypatch):
    client, store, box = setup(tmp_path)

    def boom(*a):
        raise RuntimeError("mail server down")

    monkeypatch.setattr(box, "send", boom)
    ev = event("evt_1", "checkout.session.completed", CHECKOUT)
    assert post(client, ev).status_code == 500
    assert store.db.execute("select count(*) from licenses").fetchone()[0] == 0  # nothing half-done
    assert not store.processed("evt_1")
    monkeypatch.setattr(box, "send", Mailbox().send)
    assert post(client, ev).json()["ok"]  # the retry succeeds


def test_unknown_event_ignored_and_unknown_license_404(tmp_path):
    client, store, box = setup(tmp_path)
    assert post(client, event("evt_9", "product.created", {"id": "prod_1"})).json()["result"] == "ignored"
    assert client.get("/licenses/LIC-NOPE").status_code == 404


def test_license_keys_are_unique():
    assert len({appmod.license_key() for _ in range(1000)}) == 1000
