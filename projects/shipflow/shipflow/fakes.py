"""Demo stand-ins with the real request shapes, so no real label is bought and no real order is touched:

- /carrier  a Sendcloud-style multi-carrier API: parcels (basic auth), label PDFs, a refusal for addresses the carrier
            can't route (422), and parcel status webhooks signed with Sendcloud-Signature (HMAC-SHA256)
- /shop     Shopify-style orders/paid webhooks (X-Shopify-Hmac-Sha256) and the fulfillments endpoint
/_admin endpoints (token-protected) play customers placing orders and the carrier scanning parcels.
"""
import base64, hashlib, hmac, json, os, secrets, time

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fpdf import FPDF

NAMES = {8: "DHL Paket", 1103: "DHL Express", 5: "DPD Classic"}


def label_pdf(p: dict) -> bytes:
    pdf = FPDF(format=(100, 150))
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, NAMES.get(p["shipment"]["id"], "Carrier"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    for line in (p["name"], p.get("company_name") or "", p["address"], f"{p['postal_code']} {p['city']}", p["country"], "", f"Order {p['order_number']}  {p['weight']} kg"):
        if line is not None:
            pdf.cell(0, 6, line.encode("latin-1", "replace").decode("latin-1"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Courier", "B", 14)
    pdf.ln(4)
    pdf.cell(0, 10, p["tracking_number"], new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "I", 7)
    pdf.cell(0, 6, "DEMO LABEL - not valid for shipping", new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def create_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)
    e = os.environ
    ckey, csecret, admin_token = e["CARRIER_KEY"], e["CARRIER_SECRET"], e["FAKES_ADMIN_TOKEN"]
    shop_token, shop_secret, carrier_hook, shop_hook = e["SHOP_TOKEN"], e["SHOP_WEBHOOK_SECRET"], e["CARRIER_WEBHOOK_URL"], e["SHOP_WEBHOOK_URL"]
    public = e.get("FAKES_PUBLIC_URL", "http://apis:8000").rstrip("/")
    parcels, fulfillments, orders = {}, [], {}

    def cauth(request: Request):
        if request.headers.get("authorization") != "Basic " + base64.b64encode(f"{ckey}:{csecret}".encode()).decode():
            raise HTTPException(401, {"error": {"code": 401, "message": "Unauthorized"}})

    def admin(x):
        if not secrets.compare_digest(x, admin_token):
            raise HTTPException(403)

    def post(url, body: bytes, headers: dict) -> int:
        try:
            return httpx.post(url, content=body, headers={"Content-Type": "application/json", **headers}, timeout=15).status_code
        except httpx.TransportError:
            return 0

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/carrier/api/v2/parcels")
    def find(request: Request, order_number: str = ""):
        cauth(request)
        return {"parcels": [p for p in parcels.values() if p["order_number"] == order_number]}

    @app.post("/carrier/api/v2/parcels")
    def create(request: Request, body: dict):
        cauth(request)
        p = body["parcel"]
        if (p.get("city") or "").lower() in ("nowhere", "musterstadt"):
            return JSONResponse({"error": {"code": 422, "message": {"address": f"{p['address']}, {p['postal_code']} {p['city']} could not be found by the carrier"}}}, 422)
        if p["country"] not in ("DE", "AT", "NL", "FR", "BE", "IT", "ES", "PL") and not p.get("parcel_items"):
            return JSONResponse({"error": {"code": 422, "message": {"parcel_items": "customs data is required outside the EU"}}}, 422)
        pid = 4_000_000 + len(parcels) + 1
        prefix = {8: "00340434", 1103: "JD0146", 5: "0150"}[p["shipment"]["id"]]
        tracking = prefix + secrets.token_hex(4).upper()
        parcel = {**p, "id": pid, "tracking_number": tracking, "tracking_url": f"https://tracking.example.com/{tracking}", "status": {"id": 1000, "message": "Ready to send"},
                  "label": {"label_printer": f"{public}/carrier/api/v2/labels/{pid}.pdf"}}
        parcels[pid] = parcel
        return {"parcel": parcel}

    @app.get("/carrier/api/v2/labels/{pid}.pdf")
    def label(pid: int, request: Request):
        cauth(request)
        return Response(label_pdf(parcels[pid]), media_type="application/pdf")

    @app.post("/_admin/scan")
    def scan(body: dict, x_admin: str = Header("")):
        """The carrier scans a parcel: status webhook to the app, signed like Sendcloud's."""
        admin(x_admin)
        p = next(p for p in parcels.values() if p["order_number"] == body["order_number"])
        ev = {"action": "parcel_status_changed", "id": secrets.token_hex(8), "timestamp": int(time.time() * 1000),
              "parcel": {"id": p["id"], "tracking_number": p["tracking_number"], "status": {"message": body["status"]}, "note": body.get("message", "")}}
        raw = json.dumps(ev).encode()
        return {"status": post(carrier_hook, raw, {"Sendcloud-Signature": hmac.new(csecret.encode(), raw, hashlib.sha256).hexdigest()})}

    # ---- shop ----
    @app.post("/_admin/order")
    def order(body: dict, x_admin: str = Header("")):
        admin(x_admin)
        oid = 6_000_000 + len(orders) + 1
        o = {"id": oid, "name": f"#{1000 + len(orders) + 1}", "email": body.get("email", "customer@mail.example"), "total_price": body.get("total", "99.00"),
             "shipping_address": body["address"], "line_items": [{"sku": s, "quantity": q, "title": s} for s, q in body["lines"]],
             "shipping_lines": [{"code": body.get("shipping", "standard")}]}
        orders[oid] = o
        raw = json.dumps(o).encode()
        sig = base64.b64encode(hmac.new(shop_secret.encode(), raw, hashlib.sha256).digest()).decode()
        return {"order": o, "webhook": post(shop_hook, raw, {"X-Shopify-Topic": "orders/paid", "X-Shopify-Hmac-Sha256": sig, "X-Shopify-Webhook-Id": secrets.token_hex(6)})}

    @app.post("/shop/admin/api/2026-10/orders/{oid}/fulfillments.json")
    def fulfil(oid: int, body: dict, x_shopify_access_token: str = Header("")):
        if not secrets.compare_digest(x_shopify_access_token, shop_token):
            raise HTTPException(401)
        f = {"id": len(fulfillments) + 1, "order_id": oid, **body["fulfillment"], "status": "success"}
        fulfillments.append(f)
        return {"fulfillment": f}

    @app.get("/_admin/state")
    def state(x_admin: str = Header("")):
        admin(x_admin)
        return {"parcels": list(parcels.values()), "fulfillments": fulfillments}

    return app
