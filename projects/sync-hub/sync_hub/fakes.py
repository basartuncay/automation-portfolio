"""Stand-ins for the two outside systems, so the demo and the e2e test are repeatable and touch no real account.

- /crm: a HubSpot-shaped CRM. OAuth 2 authorization-code flow with short-lived access tokens and refresh tokens,
  contacts API with an updatedAfter cursor, 429 rate limits and switchable outages.
- /shop: a Shopify-shaped store. Access-token API for customers, and webhooks signed with HMAC-SHA256 on every
  change, including changes made "in the store" through the admin endpoints below.

The /_admin endpoints exist only to drive tests and demos (token-protected). Not part of production: in real use the
hub talks to HubSpot and Shopify with the same request shapes.
"""
import base64, hashlib, hmac, html, json, os, secrets, threading, time
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse


def iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def create_app() -> FastAPI:
    e = os.environ
    admin_token = e["FAKES_ADMIN_TOKEN"]
    client_id, client_secret = e.get("CRM_CLIENT_ID", "sync-hub"), e["CRM_CLIENT_SECRET"]
    shop_token, webhook_secret = e["SHOP_ACCESS_TOKEN"], e["SHOP_WEBHOOK_SECRET"]
    token_ttl = int(e.get("CRM_TOKEN_TTL", 3600))
    app = FastAPI(docs_url=None, redoc_url=None)
    lock = threading.Lock()
    crm = {"contacts": {}, "next": 501, "codes": {}, "tokens": {}, "refresh": {}, "fail": {"mode": "ok", "count": 0},
           "stats": {"writes": 0, "refreshes": 0, "rejected_tokens": 0}}
    shop = {"customers": {}, "next": 7001, "webhook_url": e.get("SHOP_WEBHOOK_URL"), "stats": {"writes": 0, "webhooks": 0}}

    def admin(token: str):
        if not secrets.compare_digest(token, admin_token):
            raise HTTPException(403)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # ---------------- CRM ----------------
    @app.get("/crm/oauth/authorize", response_class=HTMLResponse)
    def authorize(request: Request, redirect_uri: str = "", state: str = ""):
        if request.query_params.get("client_id", "") != client_id:
            raise HTTPException(400, "unknown client_id")
        return f"""<html><body style="font:16px system-ui;max-width:460px;margin:12vh auto;padding:28px;border:1px solid #ddd;border-radius:12px">
<h2 style="margin-top:0">FakeCRM</h2><p><b>Sync Hub</b> wants to read and update your <b>contacts</b>.</p>
<form method="post" action="/crm/oauth/authorize"><input type="hidden" name="redirect_uri" value="{html.escape(redirect_uri)}">
<input type="hidden" name="state" value="{html.escape(state)}"><button style="font:16px system-ui;padding:10px 18px;background:#ff7a59;color:#fff;border:0;border-radius:8px">Allow access</button></form>
<p style="color:#888;font-size:13px">Local stand-in for a HubSpot-style OAuth consent screen.</p></body></html>"""

    @app.post("/crm/oauth/authorize")
    def consent(redirect_uri: str = Form(), state: str = Form()):
        code = secrets.token_urlsafe(16)
        crm["codes"][code] = (redirect_uri, time.time() + 300)
        return RedirectResponse(f"{redirect_uri}?{urlencode({'code': code, 'state': state})}", 303)

    def issue_tokens():
        access, refresh = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        crm["tokens"][access] = time.time() + token_ttl
        crm["refresh"][refresh] = True
        return {"access_token": access, "refresh_token": refresh, "expires_in": token_ttl, "token_type": "bearer"}

    @app.post("/crm/oauth/token")
    def token(grant_type: str = Form(), client_id_f: str = Form("", alias="client_id"), client_secret_f: str = Form("", alias="client_secret"),
              code: str = Form(""), redirect_uri: str = Form(""), refresh_token: str = Form("")):
        if client_id_f != client_id or not secrets.compare_digest(client_secret_f, client_secret):
            return JSONResponse({"error": "invalid_client"}, 401)
        with lock:
            if grant_type == "authorization_code":
                saved = crm["codes"].pop(code, None)
                if not saved or saved[0] != redirect_uri or saved[1] < time.time():
                    return JSONResponse({"error": "invalid_grant"}, 400)
                return issue_tokens()
            if grant_type == "refresh_token" and crm["refresh"].pop(refresh_token, None):  # rotating refresh tokens
                crm["stats"]["refreshes"] += 1
                return issue_tokens()
        return JSONResponse({"error": "invalid_grant"}, 400)

    def crm_auth(authorization: str):
        tok = authorization.removeprefix("Bearer ").strip()
        exp = crm["tokens"].get(tok)
        if not exp or exp < time.time():
            crm["stats"]["rejected_tokens"] += 1
            raise HTTPException(401, "expired or invalid access token")
        f = crm["fail"]
        if f["count"] > 0 and f["mode"] != "ok":
            f["count"] -= 1
            if f["mode"] == "429":
                raise HTTPException(429, "rate limited", headers={"Retry-After": "1"})
            raise HTTPException(503, "CRM is temporarily unavailable")

    def contact_out(c):
        return {"id": str(c["id"]), "properties": c["properties"], "updatedAt": c["updatedAt"]}

    @app.get("/crm/v3/objects/contacts")
    def list_contacts(updatedAfter: str = "", email: str = "", limit: int = 100, authorization: str = Header("")):
        crm_auth(authorization)
        rows = sorted((c for c in crm["contacts"].values() if c["updatedAt"] > updatedAfter
                       and (not email or c["properties"].get("email", "").lower() == email.lower())), key=lambda c: c["updatedAt"])
        return {"results": [contact_out(c) for c in rows[:limit]]}

    @app.get("/crm/v3/objects/contacts/{cid}")
    def get_contact(cid: int, authorization: str = Header("")):
        crm_auth(authorization)
        if cid not in crm["contacts"]:
            raise HTTPException(404)
        return contact_out(crm["contacts"][cid])

    def crm_write(cid, props):
        with lock:
            if cid is None:
                if any(c["properties"].get("email") == props.get("email") for c in crm["contacts"].values()):
                    raise HTTPException(409, "contact with this email already exists")
                cid = crm["next"]
                crm["next"] += 1
                crm["contacts"][cid] = {"id": cid, "properties": {}, "updatedAt": iso()}
            c = crm["contacts"][cid]
            c["properties"].update({k: v for k, v in props.items() if v is not None})
            c["updatedAt"] = iso()
            crm["stats"]["writes"] += 1
            return contact_out(c)

    @app.post("/crm/v3/objects/contacts", status_code=201)
    def create_contact(body: dict, authorization: str = Header("")):
        crm_auth(authorization)
        return crm_write(None, body.get("properties", {}))

    @app.patch("/crm/v3/objects/contacts/{cid}")
    def update_contact(cid: int, body: dict, authorization: str = Header("")):
        crm_auth(authorization)
        if cid not in crm["contacts"]:
            raise HTTPException(404)
        return crm_write(cid, body.get("properties", {}))

    @app.post("/crm/_admin/contacts/{cid}")
    def crm_admin_edit(cid: int, body: dict, x_admin: str = Header("")):
        """A sales rep editing a contact in the CRM's own UI."""
        admin(x_admin)
        return crm_write(cid, body)

    @app.post("/crm/_admin/fail")
    def crm_fail(body: dict, x_admin: str = Header("")):
        admin(x_admin)
        crm["fail"] = {"mode": body.get("mode", "ok"), "count": int(body.get("count", 0))}
        return crm["fail"]

    @app.post("/crm/_admin/expire_tokens")
    def expire(x_admin: str = Header("")):
        admin(x_admin)
        crm["tokens"] = {t: 0 for t in crm["tokens"]}
        return {"expired": len(crm["tokens"])}

    @app.get("/crm/_admin/state")
    def crm_state(x_admin: str = Header("")):
        admin(x_admin)
        return {"contacts": [contact_out(c) for c in crm["contacts"].values()], "stats": crm["stats"]}

    # ---------------- Shop ----------------
    def shop_auth(token):
        if not secrets.compare_digest(token or "", shop_token):
            raise HTTPException(401, "invalid access token")

    def send_webhook(topic, customer):
        url = shop["webhook_url"]
        if not url:
            return
        body = json.dumps(customer).encode()
        sig = base64.b64encode(hmac.new(webhook_secret.encode(), body, hashlib.sha256).digest()).decode()
        headers = {"X-Shopify-Topic": topic, "X-Shopify-Hmac-Sha256": sig, "X-Shopify-Webhook-Id": secrets.token_hex(12),
                   "Content-Type": "application/json"}

        def deliver():  # Shopify retries failed deliveries; so does this stand-in
            for attempt in range(5):
                try:
                    if httpx.post(url, content=body, headers=headers, timeout=10).status_code < 300:
                        shop["stats"]["webhooks"] += 1
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(1 + attempt)
        threading.Thread(target=deliver, daemon=True).start()

    def shop_write(cid, fields, notify=True):
        with lock:
            topic = "customers/update"
            if cid is None:
                cid, topic = shop["next"], "customers/create"
                shop["next"] += 1
                shop["customers"][cid] = {"id": cid, "email": None, "first_name": None, "last_name": None, "phone": None,
                                          "tags": "", "orders_count": 0, "total_spent": "0.00"}
            c = shop["customers"][cid]
            c.update({k: v for k, v in fields.items() if k in c and k != "id"})
            c["updated_at"] = iso()
            shop["stats"]["writes"] += 1
            out = dict(c)
        if notify:
            send_webhook(topic, out)
        return out

    @app.get("/shop/admin/api/2026-10/customers/{cid}.json")
    def shop_get(cid: int, x_shopify_access_token: str = Header("")):
        shop_auth(x_shopify_access_token)
        if cid not in shop["customers"]:
            raise HTTPException(404)
        return {"customer": shop["customers"][cid]}

    @app.get("/shop/admin/api/2026-10/customers.json")
    def shop_list(x_shopify_access_token: str = Header("")):
        shop_auth(x_shopify_access_token)
        return {"customers": list(shop["customers"].values())}

    @app.post("/shop/admin/api/2026-10/customers.json", status_code=201)
    def shop_create(body: dict, x_shopify_access_token: str = Header("")):
        shop_auth(x_shopify_access_token)
        c = body.get("customer", {})
        if any(x["email"] == c.get("email") for x in shop["customers"].values()):
            raise HTTPException(422, "email has already been taken")
        return {"customer": shop_write(None, c)}

    @app.put("/shop/admin/api/2026-10/customers/{cid}.json")
    def shop_update(cid: int, body: dict, x_shopify_access_token: str = Header("")):
        shop_auth(x_shopify_access_token)
        if cid not in shop["customers"]:
            raise HTTPException(404)
        return {"customer": shop_write(cid, body.get("customer", {}))}

    @app.post("/shop/_admin/customers")
    def shop_signup(body: dict, x_admin: str = Header("")):
        """A customer signing up in the store."""
        admin(x_admin)
        return shop_write(None, body)

    @app.post("/shop/_admin/customers/{cid}")
    def shop_admin_edit(cid: int, body: dict, x_admin: str = Header("")):
        """A store employee editing a customer; "silent": true skips the webhook (to set up a conflict on purpose)."""
        admin(x_admin)
        return shop_write(cid, {k: v for k, v in body.items() if k != "silent"}, notify=not body.get("silent"))

    @app.post("/shop/_admin/orders")
    def shop_order(body: dict, x_admin: str = Header("")):
        """A customer placing an order: orders_count and total_spent change, a customers/update webhook fires."""
        admin(x_admin)
        c = shop["customers"][int(body["customer_id"])]
        return shop_write(c["id"], {"orders_count": c["orders_count"] + 1,
                                    "total_spent": f"{float(c['total_spent']) + float(body['amount']):.2f}"})

    @app.get("/shop/_admin/state")
    def shop_state(x_admin: str = Header("")):
        admin(x_admin)
        return {"customers": list(shop["customers"].values()), "stats": shop["stats"]}

    @app.get("/_view", response_class=HTMLResponse)
    def view(token: str = ""):
        """Demo only: both systems' records side by side, to show in recordings what the hub did."""
        admin(token)
        esc = html.escape
        cust = "".join(f"<tr><td>{c['id']}</td><td>{esc(c['email'] or '')}</td><td>{esc((c['first_name'] or '') + ' ' + (c['last_name'] or ''))}</td>"
                       f"<td>{esc(c['phone'] or '')}</td><td>{c['orders_count']} / €{c['total_spent']}</td><td>{esc(c['tags'])}</td></tr>"
                       for c in shop["customers"].values())
        cont = "".join(f"<tr><td>{c['id']}</td><td>{esc(c['properties'].get('email', ''))}</td>"
                       f"<td>{esc(c['properties'].get('firstname', '') + ' ' + c['properties'].get('lastname', ''))}</td>"
                       f"<td>{esc(c['properties'].get('phone', ''))}</td><td>{esc(c['properties'].get('lifecyclestage', ''))} / €{esc(str(c['properties'].get('total_spent', '')))}</td>"
                       f"<td>{'VIP' if c['properties'].get('vip') == 'true' else ''}</td></tr>" for c in crm["contacts"].values())
        head = "<tr><th>id</th><th>email</th><th>name</th><th>phone</th><th>{}</th><th>{}</th></tr>"
        style = ("body{font:15px system-ui;margin:28px;background:#f6f7f9} h2{margin:0 0 8px} .w{display:grid;grid-template-columns:1fr 1fr;gap:20px}"
                 " table{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden} th,td{padding:9px 10px;border-bottom:1px solid #e5e7eb;text-align:left;font-size:14px}"
                 " th{background:#f3f4f6;font-size:12px;color:#6b7280} .s h2{color:#16a34a} .c h2{color:#ff7a59}")
        return (f"<html><head><meta http-equiv='refresh' content='2'><style>{style}</style></head><body><div class='w'>"
                f"<div class='s'><h2>Store (Shopify-style)</h2><table>{head.format('orders / spent', 'tags')}{cust}</table></div>"
                f"<div class='c'><h2>CRM (HubSpot-style)</h2><table>{head.format('stage / spent', 'vip')}{cont}</table></div></div></body></html>")

    return app
