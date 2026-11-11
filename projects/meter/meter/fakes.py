"""Demo stand-in for Stripe Billing with the real request shapes (form-encoded, basic auth, Idempotency-Key):
customers, subscriptions with a flat and a metered price, Billing Meter events (deduplicated by identifier) and
event summaries, prorations, invoices at period end with included usage priced like a graduated tier, card failures
and retries, and signed webhooks. /_admin endpoints (token-protected) close periods and play the card network."""
import hashlib, hmac, json, math, os, secrets, time

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

PRICES = {"price_starter_base": 2900, "price_pro_base": 9900, "price_scale_base": 49900}
METERED = {"price_api_calls_starter": (10_000, 200), "price_api_calls_pro": (100_000, 150), "price_api_calls_scale": (1_000_000, 90)}
DAY = 86400


def create_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)
    key, whsec, admin_token, hook = os.environ["STRIPE_SECRET_KEY"], os.environ["STRIPE_WEBHOOK_SECRET"], os.environ["FAKES_ADMIN_TOKEN"], os.environ["STRIPE_WEBHOOK_URL"]
    customers, subs, invoices, idem, meter, seen = {}, {}, {}, {}, [], set()
    cards, prorations = {}, {}

    def auth(request: Request):
        import base64
        if request.headers.get("authorization") != "Basic " + base64.b64encode(f"{key}:".encode()).decode():
            raise HTTPException(401, {"error": {"message": "Invalid API Key provided"}})

    def admin(x):
        if not secrets.compare_digest(x, admin_token):
            raise HTTPException(403)

    def send(type_: str, obj: dict) -> int:
        body = json.dumps({"id": "evt_" + secrets.token_hex(10), "type": type_, "created": int(time.time()), "data": {"object": obj}}).encode()
        t = int(time.time())
        sig = f"t={t},v1={hmac.new(whsec.encode(), f'{t}.'.encode() + body, hashlib.sha256).hexdigest()}"
        try:
            return httpx.post(hook, content=body, headers={"Content-Type": "application/json", "Stripe-Signature": sig}, timeout=15).status_code
        except httpx.TransportError:
            return 0

    def once(request: Request, make):
        k = request.headers.get("idempotency-key")
        if k and k in idem:
            return idem[k]
        out = make()
        if k:
            idem[k] = out
        return out

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.post("/v1/customers")
    async def create_customer(request: Request):
        auth(request)
        f = dict(await request.form())
        def make():
            c = {"id": "cus_" + secrets.token_hex(7), "object": "customer", "name": f.get("name"), "email": f.get("email")}
            customers[c["id"]] = c
            cards[c["id"]] = True
            return c
        return once(request, make)

    @app.post("/v1/subscriptions")
    async def create_sub(request: Request):
        auth(request)
        f = dict(await request.form())
        def make():
            start = int(time.time()) // DAY * DAY
            s = {"id": "sub_" + secrets.token_hex(7), "object": "subscription", "customer": f["customer"], "status": "active",
                 "items": [f["items[0][price]"], f["items[1][price]"]], "current_period_start": start, "current_period_end": start + 30 * DAY}
            subs[s["id"]] = s
            return s
        return once(request, make)

    @app.post("/v1/subscriptions/{sid}")
    async def update_sub(sid: str, request: Request):
        auth(request)
        f = dict(await request.form())
        def make():
            s = subs[sid]
            old_base, new_base = PRICES[s["items"][0]], PRICES[f["items[0][price]"]]
            if f.get("proration_behavior") == "create_prorations":
                left = (s["current_period_end"] - time.time()) / (s["current_period_end"] - s["current_period_start"])
                prorations.setdefault(s["customer"], []).append({"description": "Remaining time on the new plan", "amount": round((new_base - old_base) * left)})
            s["items"] = [f["items[0][price]"], f["items[1][price]"]]
            return s
        return once(request, make)

    @app.post("/v1/billing/meter_events")
    async def meter_event(request: Request):
        auth(request)
        f = dict(await request.form())
        if f["identifier"] in seen:  # Stripe ignores a repeated identifier
            return {"object": "billing.meter_event", "identifier": f["identifier"], "duplicate": True}
        seen.add(f["identifier"])
        meter.append({"customer": f["payload[stripe_customer_id]"], "value": int(f["payload[value]"]), "ts": int(f["timestamp"])})
        return {"object": "billing.meter_event", "identifier": f["identifier"]}

    @app.get("/v1/billing/meters/{mid}/event_summaries")
    def summaries(mid: str, customer: str, start_time: int, end_time: int, request: Request):
        auth(request)
        total = sum(m["value"] for m in meter if m["customer"] == customer and start_time <= m["ts"] < end_time)
        return {"object": "list", "data": [{"object": "billing.meter_event_summary", "aggregated_value": total, "start_time": start_time, "end_time": end_time}]}

    def attempt(inv: dict) -> int:
        inv["attempt_count"] += 1
        if cards.get(inv["customer"], True):
            inv.update(status="paid", amount_paid=inv["amount_due"])
            return send("invoice.paid", inv)
        return send("invoice.payment_failed", inv)

    @app.post("/_admin/close_period")
    def close_period(body: dict, x_admin: str = Header("")):
        """The billing period ends: invoice the flat fee, prorations and usage beyond what's included, then renew."""
        admin(x_admin)
        s = next(s for s in subs.values() if s["customer"] == body["customer"])
        used = sum(m["value"] for m in meter if m["customer"] == s["customer"] and s["current_period_start"] <= m["ts"] < s["current_period_end"])
        included, per_1k = METERED[s["items"][1]]
        lines = [{"description": "Plan", "amount": PRICES[s["items"][0]]}] + prorations.pop(s["customer"], [])
        if used > included:
            lines.append({"description": f"{used - included:,} calls over {included:,}", "amount": math.ceil((used - included) / 1000) * per_1k})
        inv = {"id": "in_" + secrets.token_hex(8), "object": "invoice", "customer": s["customer"], "subscription": s["id"], "number": f"MTR-{len(invoices) + 1001}",
               "status": "open", "amount_due": sum(l["amount"] for l in lines), "amount_paid": 0, "lines": lines, "attempt_count": 0,
               "period_start": s["current_period_start"], "period_end": s["current_period_end"], "hosted_invoice_url": f"https://invoice.stripe.example/{len(invoices) + 1001}"}
        invoices[inv["id"]] = inv
        send("invoice.finalized", inv)
        attempt(inv)
        span = s["current_period_end"] - s["current_period_start"]
        s["current_period_start"], s["current_period_end"] = s["current_period_end"], s["current_period_end"] + span
        send("customer.subscription.updated", s)
        return inv

    @app.post("/_admin/card")
    def card(body: dict, x_admin: str = Header("")):
        admin(x_admin)
        cards[body["customer"]] = body["ok"]
        return {"ok": body["ok"]}

    @app.post("/_admin/retry")
    def retry(body: dict, x_admin: str = Header("")):
        admin(x_admin)
        return {"webhook": attempt(invoices[body["invoice"]]), "invoice": invoices[body["invoice"]]}

    @app.post("/_admin/replay")
    def replay(body: dict, x_admin: str = Header("")):
        """A webhook with an hour-old timestamp: a replay."""
        admin(x_admin)
        raw = json.dumps({"id": "evt_old", "type": "invoice.paid", "data": {"object": {}}}).encode()
        t = int(time.time()) - 3600
        sig = f"t={t},v1={hmac.new(whsec.encode(), f'{t}.'.encode() + raw, hashlib.sha256).hexdigest()}"
        return {"status": httpx.post(hook, content=raw, headers={"Stripe-Signature": sig}, timeout=15).status_code}

    @app.get("/_admin/state")
    def state(x_admin: str = Header("")):
        admin(x_admin)
        return {"customers": customers, "subscriptions": subs, "invoices": invoices, "meter_events": len(meter), "metered": {c: sum(m["value"] for m in meter if m["customer"] == c) for c in customers}}

    return app
