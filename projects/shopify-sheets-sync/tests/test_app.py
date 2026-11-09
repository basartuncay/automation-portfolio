import base64, hashlib, hmac, json

from fastapi.testclient import TestClient

from shopify_sync.app import MemorySheet, SeenWebhooks, create_app

SECRET = "test-secret"
ORDER = {
    "id": 5551234, "name": "#1001", "created_at": "2026-11-09T10:00:00+00:00",
    "financial_status": "paid", "fulfillment_status": None, "currency": "USD",
    "subtotal_price": "50.00", "total_tax": "4.00", "total_price": "54.00",
    "email": "jane@example.com", "customer": {"first_name": "Jane"},
    "line_items": [{"title": "Mug", "quantity": 2}, {"title": "Tea", "quantity": 1}],
    "shipping_address": {"country_code": "DE", "address1": "Secret street 1"},
}


def post(client, order, webhook_id="w1", topic="orders/create", secret=SECRET):
    body = json.dumps(order).encode()
    sig = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return client.post("/webhooks/orders", content=body, headers={
        "X-Shopify-Hmac-Sha256": sig, "X-Shopify-Webhook-Id": webhook_id, "X-Shopify-Topic": topic,
        "Content-Type": "application/json"})


def setup():
    sheet = MemorySheet()
    return TestClient(create_app(SECRET, sheet, SeenWebhooks())), sheet


def test_valid_order_becomes_row_without_personal_data():
    client, sheet = setup()
    assert post(client, ORDER).status_code == 200
    row = sheet.rows["5551234"]
    assert row[:3] == ["5551234", "#1001", "2026-11-09T10:00:00+00:00"]
    assert row[9:] == ["2x Mug; 1x Tea", 3, "DE"]
    assert "jane@example.com" not in json.dumps(row) and "Secret street" not in json.dumps(row)


def test_bad_signature_rejected():
    client, sheet = setup()
    assert post(client, ORDER, secret="wrong").status_code == 401
    assert sheet.rows == {}


def test_retried_webhook_processed_once():
    client, sheet = setup()
    post(client, ORDER, webhook_id="same")
    changed = {**ORDER, "total_price": "999.00"}
    assert post(client, changed, webhook_id="same").json() == {"duplicate": True}
    assert sheet.rows["5551234"][8] == "54.00"


def test_update_overwrites_same_row():
    client, sheet = setup()
    post(client, ORDER, webhook_id="a")
    post(client, {**ORDER, "fulfillment_status": "fulfilled"}, webhook_id="b", topic="orders/updated")
    assert len(sheet.rows) == 1 and sheet.rows["5551234"][4] == "fulfilled"


def test_other_topics_ignored():
    client, sheet = setup()
    assert post(client, ORDER, topic="products/create").json() == {"ignored": "products/create"}
    assert sheet.rows == {}
