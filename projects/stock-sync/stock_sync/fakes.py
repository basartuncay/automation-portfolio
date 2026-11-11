"""Demo stand-ins with the real request shapes, so nothing touches a real store, seller account or warehouse:

- /shopify  Admin REST inventory levels; customer orders are refused when the shop shows too little stock (like a
            real storefront), and orders/create + orders/cancelled webhooks are signed with X-Shopify-Hmac-Sha256
- /amazon   SP-API Listings Items (fulfillment_availability) and Orders (getOrders with LastUpdatedAfter + NextToken,
            getOrderItems); throttles with 429 on demand
- /wms      the warehouse's physical stock; receipts and shipments are sent as signed webhooks
/_admin endpoints (token-protected) play the customers, the warehouse staff and the outages.
"""
import base64, hashlib, hmac, json, os, secrets, time
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .db import CATALOG, LISTINGS


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)
    e = os.environ
    shop_token, amz_token, wms_token, admin_token = e["SHOPIFY_TOKEN"], e["AMAZON_TOKEN"], e["WMS_TOKEN"], e["FAKES_ADMIN_TOKEN"]
    shop_secret, wms_secret, hub = e["SHOPIFY_WEBHOOK_SECRET"], e["WMS_WEBHOOK_SECRET"], e["HUB_URL"].rstrip("/")
    shop_sku = {ref: sku for ch, sku, ref, *_ in LISTINGS if ch == "shopify"}
    shop = {"levels": {ref: 0 for ref in shop_sku}, "orders": {}, "next": 1001, "writes": 0}
    amz = {"qty": {ref: 0 for ch, _, ref, *_ in LISTINGS if ch == "amazon"}, "orders": [], "throttle": 0, "writes": 0}
    wms = {"stock": {sku: qty for sku, _, qty, _ in CATALOG}, "next": 1}
    hits = {"shopify": 0}

    def admin(x: str):
        if not secrets.compare_digest(x, admin_token):
            raise HTTPException(403)

    def shopify_auth(token: str):
        if not secrets.compare_digest(token, shop_token):
            raise HTTPException(401, "[API] Invalid API key or access token")

    def amazon_auth(token: str):
        if not secrets.compare_digest(token, amz_token):
            raise HTTPException(403, "Access to requested resource is denied.")
        if amz["throttle"] > 0:
            amz["throttle"] -= 1
            raise HTTPException(429, "You exceeded your quota for the requested resource.", headers={"Retry-After": "1"})

    def send_shopify(topic: str, order: dict):
        body = json.dumps(order).encode()
        sig = base64.b64encode(hmac.new(shop_secret.encode(), body, hashlib.sha256).digest()).decode()
        return httpx.post(f"{hub}/webhooks/shopify", content=body, timeout=15, headers={
            "Content-Type": "application/json", "X-Shopify-Topic": topic, "X-Shopify-Hmac-Sha256": sig, "X-Shopify-Webhook-Id": secrets.token_hex(8)}).status_code

    def send_wms(event: dict, signature: str | None = None):
        body = json.dumps(event).encode()
        sig = signature or hmac.new(wms_secret.encode(), body, hashlib.sha256).hexdigest()
        return httpx.post(f"{hub}/webhooks/wms", content=body, timeout=15, headers={"Content-Type": "application/json", "X-WMS-Signature": sig}).status_code

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # ---- Shopify ----
    @app.post("/shopify/admin/api/2026-10/inventory_levels/set.json")
    def shop_set(body: dict, x_shopify_access_token: str = Header("")):
        shopify_auth(x_shopify_access_token)
        if body["inventory_item_id"] not in shop["levels"]:
            raise HTTPException(404, "Not Found")
        shop["levels"][body["inventory_item_id"]] = int(body["available"])
        shop["writes"] += 1
        return {"inventory_level": {"inventory_item_id": body["inventory_item_id"], "location_id": body["location_id"], "available": int(body["available"])}}

    @app.get("/shopify/admin/api/2026-10/inventory_levels.json")
    def shop_levels(inventory_item_ids: str, x_shopify_access_token: str = Header("")):
        shopify_auth(x_shopify_access_token)
        return {"inventory_levels": [{"inventory_item_id": r, "available": shop["levels"].get(r, 0)} for r in inventory_item_ids.split(",")]}

    # ---- Amazon SP-API ----
    @app.patch("/amazon/listings/2021-08-01/items/{seller}/{sku}")
    def amz_patch(seller: str, sku: str, body: dict, x_amz_access_token: str = Header("")):
        amazon_auth(x_amz_access_token)
        if sku not in amz["qty"]:
            return JSONResponse({"sku": sku, "status": "INVALID", "issues": [{"code": "4000001", "message": "SKU not found", "severity": "ERROR"}]}, 400)
        amz["qty"][sku] = int(body["patches"][0]["value"][0]["quantity"])
        amz["writes"] += 1
        return {"sku": sku, "status": "ACCEPTED", "submissionId": secrets.token_hex(8), "issues": []}

    @app.get("/amazon/listings/2021-08-01/items/{seller}/{sku}")
    def amz_get(seller: str, sku: str, x_amz_access_token: str = Header("")):
        amazon_auth(x_amz_access_token)
        return {"sku": sku, "fulfillmentAvailability": [{"fulfillmentChannelCode": "DEFAULT", "quantity": amz["qty"].get(sku, 0)}]}

    @app.get("/amazon/orders/v0/orders")
    def amz_orders(LastUpdatedAfter: str, NextToken: str = "", x_amz_access_token: str = Header("")):
        amazon_auth(x_amz_access_token)
        rows = [o for o in amz["orders"] if o["LastUpdateDate"] >= LastUpdatedAfter]
        start = int(NextToken or 0)
        page = rows[start:start + 2]  # small pages so pagination is exercised
        payload = {"Orders": [{k: v for k, v in o.items() if k != "items"} for o in page]}
        if start + 2 < len(rows):
            payload["NextToken"] = str(start + 2)
        return {"payload": payload}

    @app.get("/amazon/orders/v0/orders/{oid}/orderItems")
    def amz_items(oid: str, x_amz_access_token: str = Header("")):
        amazon_auth(x_amz_access_token)
        o = next(o for o in amz["orders"] if o["AmazonOrderId"] == oid)
        return {"payload": {"AmazonOrderId": oid, "OrderItems": o["items"]}}

    # ---- Warehouse ----
    @app.get("/wms/v1/stock")
    def wms_stock(authorization: str = Header("")):
        if not secrets.compare_digest(authorization.removeprefix("Bearer "), wms_token):
            raise HTTPException(401)
        return {"items": [{"sku": k, "on_hand": v} for k, v in sorted(wms["stock"].items())], "at": stamp()}

    # ---- the outside world ----
    @app.post("/_admin/shopify/order")
    def shop_buy(body: dict, x_admin: str = Header("")):
        """A customer checks out. The storefront refuses quantities it doesn't show, like a real shop."""
        admin(x_admin)
        ref = {v: k for k, v in shop_sku.items()}
        for l in body["lines"]:
            if shop["levels"][ref[l["sku"]]] < l["qty"]:
                return JSONResponse({"error": f"{l['sku']} is sold out on the storefront"}, 409)
        for l in body["lines"]:
            shop["levels"][ref[l["sku"]]] -= l["qty"]
        n = shop["next"]; shop["next"] += 1
        order = {"id": 5_000_000 + n, "name": f"#{n}", "created_at": stamp(), "cancelled_at": None,
                 "line_items": [{"sku": l["sku"], "quantity": l["qty"]} for l in body["lines"]]}
        shop["orders"][order["id"]] = order
        status = send_shopify("orders/create", order) if not body.get("no_webhook") else None
        return {"order": order, "webhook": status}

    @app.post("/_admin/shopify/cancel")
    def shop_cancel(body: dict, x_admin: str = Header("")):
        admin(x_admin)
        order = shop["orders"][int(body["id"])]
        order["cancelled_at"] = stamp()
        return {"webhook": send_shopify("orders/cancelled", order)}

    @app.post("/_admin/shopify/webhook")
    def shop_resend(body: dict, x_admin: str = Header("")):
        admin(x_admin)
        return {"webhook": send_shopify(body["topic"], shop["orders"][int(body["id"])])}

    @app.post("/_admin/shopify/set")
    def shop_manual(body: dict, x_admin: str = Header("")):
        """Someone edits a quantity by hand in the Shopify admin."""
        admin(x_admin)
        shop["levels"][body["ref"]] = int(body["qty"])
        return shop["levels"]

    @app.post("/_admin/amazon/order")
    def amz_buy(body: dict, x_admin: str = Header("")):
        admin(x_admin)
        for l in body["lines"]:
            if amz["qty"].get(l["sku"], 0) < l["qty"]:
                return JSONResponse({"error": f"{l['sku']} is unavailable on Amazon"}, 409)
        for l in body["lines"]:
            amz["qty"][l["sku"]] -= l["qty"]
        oid = f"302-{len(amz['orders']) + 1:07d}-{secrets.randbelow(10**7):07d}"
        amz["orders"].append({"AmazonOrderId": oid, "OrderStatus": "Unshipped", "PurchaseDate": stamp(), "LastUpdateDate": stamp(),
                              "items": [{"SellerSKU": l["sku"], "QuantityOrdered": l["qty"]} for l in body["lines"]]})
        return {"AmazonOrderId": oid}

    @app.post("/_admin/amazon/cancel")
    def amz_cancel(body: dict, x_admin: str = Header("")):
        admin(x_admin)
        o = next(o for o in amz["orders"] if o["AmazonOrderId"] == body["id"])
        o["OrderStatus"], o["LastUpdateDate"] = "Canceled", stamp()
        return o

    @app.post("/_admin/amazon/throttle")
    def amz_throttle(body: dict, x_admin: str = Header("")):
        admin(x_admin)
        amz["throttle"] = int(body["count"])
        return {"throttle": amz["throttle"]}

    @app.post("/_admin/wms/receive")
    def wms_receive(body: dict, x_admin: str = Header("")):
        admin(x_admin)
        wms["stock"][body["sku"]] += int(body["qty"])
        rid = f"GR-{wms['next']}"; wms["next"] += 1
        return {"webhook": send_wms({"type": "receipt", "receipt_id": rid, "sku": body["sku"], "qty": int(body["qty"])}, body.get("signature"))}

    @app.post("/_admin/wms/ship")
    def wms_ship(body: dict, x_admin: str = Header("")):
        admin(x_admin)
        for sku, qty in body["units"].items():
            wms["stock"][sku] -= qty
        sid = f"SH-{wms['next']}"; wms["next"] += 1
        return {"webhook": send_wms({"type": "shipment", "shipment_id": sid, "order_ref": body["order_ref"]})}

    @app.post("/_admin/wms/count")
    def wms_count(body: dict, x_admin: str = Header("")):
        """A stock count finds a different number (breakage, theft, a mis-pick). No webhook: only reconciliation sees it."""
        admin(x_admin)
        wms["stock"][body["sku"]] = int(body["on_hand"])
        return wms["stock"]

    @app.get("/_admin/state")
    def state(x_admin: str = Header("")):
        admin(x_admin)
        return {"shopify": {"levels": {shop_sku[k]: v for k, v in shop["levels"].items()}, "writes": shop["writes"]},
                "amazon": {"qty": amz["qty"], "writes": amz["writes"], "orders": len(amz["orders"])}, "wms": wms["stock"]}

    return app
