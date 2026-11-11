"""Stripe client for usage billing (form-encoded like the real API, secret key as basic auth, Idempotency-Key on
writes) and webhook signature verification (Stripe-Signature: t=..,v1=.. over "t.payload", 5-minute tolerance)."""
import hashlib, hmac, os, time

import httpx


class StripeError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def flat(data: dict, prefix: str = "") -> dict:
    """Stripe's form encoding: {"items": [{"price": "x"}]} -> {"items[0][price]": "x"}."""
    out = {}
    for k, v in data.items():
        key = f"{prefix}[{k}]" if prefix else k
        if isinstance(v, dict):
            out.update(flat(v, key))
        elif isinstance(v, list):
            for i, item in enumerate(v):
                out.update(flat(item, f"{key}[{i}]") if isinstance(item, dict) else {f"{key}[{i}]": str(item)})
        elif v is not None:
            out[key] = str(v).lower() if isinstance(v, bool) else str(v)
    return out


class Stripe:
    def __init__(self, http: httpx.Client | None = None):
        self.base = os.environ.get("STRIPE_URL", "https://api.stripe.com").rstrip("/")
        self.key = os.environ["STRIPE_SECRET_KEY"]
        self.http = http or httpx.Client(timeout=20)

    def _call(self, method: str, path: str, data: dict | None = None, idem: str | None = None, params: dict | None = None) -> dict:
        h = {"Idempotency-Key": idem} if idem else {}
        try:
            r = self.http.request(method, f"{self.base}{path}", data=flat(data or {}), params=params, auth=(self.key, ""), headers=h)
        except httpx.TransportError as e:
            raise StripeError(0, f"network error: {e}")
        if r.status_code >= 400:
            raise StripeError(r.status_code, r.json().get("error", {}).get("message", r.text[:200]))
        return r.json()

    def create_customer(self, ref: str, name: str, email: str) -> dict:
        return self._call("POST", "/v1/customers", {"name": name, "email": email, "metadata": {"ref": ref}}, idem=f"cus-{ref}")

    def create_subscription(self, customer: str, base_price: str, metered_price: str, ref: str) -> dict:
        return self._call("POST", "/v1/subscriptions", {"customer": customer, "items": [{"price": base_price}, {"price": metered_price}],
                                                        "payment_behavior": "default_incomplete", "metadata": {"ref": ref}}, idem=f"sub-{ref}")

    def change_prices(self, sub: str, base_price: str, metered_price: str, proration: str, idem: str) -> dict:
        return self._call("POST", f"/v1/subscriptions/{sub}", {"items": [{"price": base_price}, {"price": metered_price}], "proration_behavior": proration}, idem=idem)

    def meter_event(self, stripe_customer: str, value: int, identifier: str, ts: int) -> dict:
        return self._call("POST", "/v1/billing/meter_events", {"event_name": "api_calls", "identifier": identifier, "timestamp": ts,
                                                               "payload": {"stripe_customer_id": stripe_customer, "value": value}})

    def meter_total(self, stripe_customer: str, start: int, end: int) -> int:
        r = self._call("GET", f"/v1/billing/meters/{os.environ.get('STRIPE_METER_ID', 'mtr_api_calls')}/event_summaries",
                       params={"customer": stripe_customer, "start_time": start, "end_time": end})
        return sum(s["aggregated_value"] for s in r["data"])


def verify(payload: bytes, header: str, secret: str, tolerance: int = 300) -> bool:
    parts = [p.split("=", 1) for p in header.split(",") if "=" in p]
    t = next((v for k, v in parts if k == "t"), "0")
    if not t.isdigit() or abs(time.time() - int(t)) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(k == "v1" and hmac.compare_digest(expected, v) for k, v in parts)
