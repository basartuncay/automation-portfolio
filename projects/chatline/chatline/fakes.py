"""Demo stand-ins with the real request shapes, so nothing touches a real WhatsApp number or shop:

- /graph   WhatsApp Cloud API: POST /v21.0/{phone_number_id}/messages. Enforces the rules that bite in production:
           3 reply buttons max, 20-character titles, the 24-hour window (error 131047), approved templates only (132001).
           Delivery statuses and customers' messages are sent to the app's webhook signed with X-Hub-Signature-256.
- /phone/{wa_id}  the customer's phone: chat bubbles with tappable buttons and lists, for the demo
- /shop    orders by phone or number, idempotent return creation; "shipped" events as signed webhooks
/_admin endpoints (token-protected) play the customers and the warehouse.
"""
import hashlib, hmac, html, json, os, secrets, time
from datetime import date, timedelta

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

APPROVED_TEMPLATES = {"order_shipped"}
PROFILES = {"4917612345678": "Lena Hoffmann", "4917699887766": "Max Weber"}  # the WhatsApp profile names the demo customers use


def orders_seed() -> dict:
    d = lambda n: (date.today() - timedelta(days=n)).isoformat()
    rows = [
        ("1042", "4917612345678", "10115", "shipped", d(2), None, [("HP-X200", "Wireless Headphones X200", 128.99)]),
        ("1031", "4917612345678", "10115", "delivered", d(12), d(9), [("SNK-42", "Runner sneakers, size 42", 89.0), ("SOCK-3", "Socks, 3 pairs", 12.0)]),
        ("1050", "4917699887766", "80331", "processing", d(0), None, [("MUG-BLK", "Enamel mug, black", 14.0)]),
        ("0977", "4917699887766", "80331", "delivered", d(60), d(55), [("KTL-G", "Gooseneck kettle", 49.0)]),
    ]
    return {no: {"number": no, "phone": ph, "postcode": pc, "status": st, "placed": placed, "delivered": dl, "carrier": "DHL",
                 "tracking_url": f"https://track.example.com/{no}", "items": [{"sku": s, "title": t, "price": p} for s, t, p in items]}
            for no, ph, pc, st, placed, dl, items in rows}


def create_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)
    e = os.environ
    token, app_secret, admin_token, number_id = e["WA_ACCESS_TOKEN"], e["WA_APP_SECRET"], e["FAKES_ADMIN_TOKEN"], e["WA_PHONE_NUMBER_ID"]
    shop_token, shop_secret, hook, shop_hook = e["SHOP_TOKEN"], e["SHOP_WEBHOOK_SECRET"], e["WA_WEBHOOK_URL"], e["SHOP_WEBHOOK_URL"]
    chats: dict[str, list] = {}
    last_in: dict[str, float] = {}
    orders, returns = orders_seed(), {}

    def admin(x):
        if not secrets.compare_digest(x, admin_token):
            raise HTTPException(403)

    def to_app(value: dict):
        body = json.dumps({"object": "whatsapp_business_account", "entry": [{"id": "WABA1", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp", "metadata": {"display_phone_number": "4930123456", "phone_number_id": number_id}, **value}}]}]}).encode()
        sig = "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
        try:
            return httpx.post(hook, content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig}, timeout=15).status_code
        except httpx.TransportError:
            return 0  # the app isn't reachable (unit tests feed the webhook themselves)

    def err(status: int, code: int, message: str):
        return JSONResponse({"error": {"message": f"(#{code}) {message}", "type": "OAuthException", "code": code, "fbtrace_id": secrets.token_hex(6)}}, status)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.post("/graph/v21.0/{pid}/messages")
    def send(pid: str, body: dict, authorization: str = Header("")):
        if pid != number_id or not secrets.compare_digest(authorization.removeprefix("Bearer "), token):
            return err(401, 190, "Invalid OAuth access token.")
        to, t = body["to"], body["type"]
        if t != "template" and time.time() - last_in.get(to, 0) > 24 * 3600:
            return err(400, 131047, "Re-engagement message: more than 24 hours have passed since the recipient last replied.")
        if t == "template" and body["template"]["name"] not in APPROVED_TEMPLATES:
            return err(404, 132001, "Template name does not exist in the translation")
        if t == "interactive" and body["interactive"]["type"] == "button":
            bs = body["interactive"]["action"]["buttons"]
            if len(bs) > 3 or any(len(b["reply"]["title"]) > 20 for b in bs):
                return err(400, 131009, "Parameter value is not valid")
        wamid = "wamid." + secrets.token_hex(12)
        chats.setdefault(to, []).append({"dir": "out", "id": wamid, "msg": body, "at": time.time()})
        for st in ("sent", "delivered", "read"):  # a real phone reports these over a few seconds; here, right away
            to_app({"statuses": [{"id": wamid, "status": st, "timestamp": str(int(time.time())), "recipient_id": to}]})
        return {"messaging_product": "whatsapp", "contacts": [{"input": to, "wa_id": to}], "messages": [{"id": wamid}]}

    def customer_message(wa_id: str, name: str, message: dict, age_hours: float = 0):
        ts = time.time() - age_hours * 3600
        last_in[wa_id] = max(last_in.get(wa_id, 0), ts)
        msg = {"from": wa_id, "id": "wamid.in" + secrets.token_hex(10), "timestamp": str(int(ts)), **message}
        chats.setdefault(wa_id, []).append({"dir": "in", "id": msg["id"], "msg": msg, "at": ts})
        r = to_app({"contacts": [{"profile": {"name": name}, "wa_id": wa_id}], "messages": [msg]})
        return {"status": r, "id": msg["id"], "msg": msg}

    @app.post("/_admin/say")
    def say(body: dict, x_admin: str = Header("")):
        admin(x_admin)
        return customer_message(body["from"], body.get("name", ""), {"type": "text", "text": {"body": body["text"]}}, body.get("age_hours", 0))

    @app.post("/_admin/tap")
    def tap(body: dict, x_admin: str = Header("")):
        """The customer taps a reply button or a list row in the last interactive message."""
        admin(x_admin)
        kind = "list_reply" if body.get("list") else "button_reply"
        return customer_message(body["from"], body.get("name", ""), {"type": "interactive", "interactive": {"type": kind, kind: {"id": body["id"], "title": body["title"]}}})

    @app.post("/_admin/resend")
    def resend(body: dict, x_admin: str = Header("")):
        """WhatsApp delivers the same webhook again (it does, on timeouts)."""
        admin(x_admin)
        m = next(c["msg"] for c in chats[body["from"]] if c["id"] == body["id"])
        return {"status": to_app({"contacts": [{"profile": {"name": ""}, "wa_id": body["from"]}], "messages": [m]})}

    @app.get("/_admin/chat/{wa_id}")
    def chat(wa_id: str, x_admin: str = Header("")):
        admin(x_admin)
        return chats.get(wa_id, [])

    @app.get("/phone/{wa_id}", response_class=HTMLResponse)
    def phone(wa_id: str, request: Request):
        if not secrets.compare_digest(request.query_params.get("token", ""), admin_token):
            raise HTTPException(403)
        out = []
        for c in chats.get(wa_id, []):
            m = c["msg"]
            if c["dir"] == "in":
                txt = m["text"]["body"] if m["type"] == "text" else (m["interactive"].get("button_reply") or m["interactive"].get("list_reply"))["title"]
                out.append(f'<div class="b me">{html.escape(txt)}</div>')
                continue
            if m["type"] == "text":
                out.append(f'<div class="b">{html.escape(m["text"]["body"]).replace(chr(10), "<br>")}</div>')
            elif m["type"] == "template":
                p = [x["text"] for x in m["template"]["components"][0]["parameters"]]
                out.append(f'<div class="b"><i>Update</i><br>Your order {html.escape(p[0])} has shipped with {html.escape(p[1])}. Track it: {html.escape(p[2])}</div>')
            else:
                i = m["interactive"]
                opts = [(b["reply"]["id"], b["reply"]["title"], False) for b in i["action"].get("buttons", [])] or \
                       [(r["id"], r["title"], True) for s in i["action"].get("sections", []) for r in s["rows"]]
                btns = "".join(f'<button data-id="{html.escape(o)}" data-title="{html.escape(t)}" data-list="{int(l)}">{"☰ " if l else ""}{html.escape(t)}</button>' for o, t, l in opts)
                out.append(f'<div class="b">{html.escape(i["body"]["text"])}<div class="opts">{btns}</div></div>')
        return f"""<!doctype html><meta charset=utf-8><title>WhatsApp · {wa_id}</title>
<style>body{{margin:0;background:#d1d5db;font:15px/1.4 system-ui;display:flex;justify-content:center;padding:20px}}
.p{{width:380px;height:760px;background:#efeae2;border-radius:36px;border:10px solid #111;display:flex;flex-direction:column;overflow:hidden}}
.h{{background:#075e54;color:#fff;padding:14px 18px;font-weight:600}} .h small{{display:block;font-weight:400;opacity:.8}}
.c{{flex:1;overflow:auto;padding:12px;display:flex;flex-direction:column;gap:8px}} .b{{max-width:82%;background:#fff;border-radius:10px;padding:8px 10px;align-self:flex-start;box-shadow:0 1px 1px #0001}}
.b.me{{background:#d9fdd3;align-self:flex-end}} .opts{{display:flex;flex-direction:column;gap:4px;margin-top:8px}}
.opts button{{border:0;border-top:1px solid #e5e7eb;background:none;color:#027eb5;font:600 14px system-ui;padding:7px;cursor:pointer}}
.in{{display:flex;gap:8px;padding:10px;background:#f0f2f5}} .in input{{flex:1;border:0;border-radius:18px;padding:9px 14px;font:inherit}} .in button{{border:0;background:#00a884;color:#fff;border-radius:50%;width:38px}}</style>
<div class=p><div class=h>Kiku Shop<small>Business account · test number</small></div><div class=c id=c>{''.join(out)}</div>
<form class=in id=f><input id=t placeholder="Message" autocomplete=off><button>➤</button></form></div>
<script>
const T = new URLSearchParams(location.search).get('token'), W = {json.dumps(wa_id)}, N = {json.dumps(PROFILES.get(wa_id, "Customer"))};
const post = (p, b) => fetch(p, {{method: 'POST', headers: {{'Content-Type': 'application/json', 'x-admin': T}}, body: JSON.stringify(b)}}).then(() => setTimeout(() => location.reload(), 600));
document.querySelectorAll('.opts button').forEach(b => b.onclick = () => post('/_admin/tap', {{from: W, name: N, id: b.dataset.id, title: b.dataset.title, list: b.dataset.list === '1'}}));
document.getElementById('f').onsubmit = e => {{ e.preventDefault(); const v = document.getElementById('t').value; if (v) post('/_admin/say', {{from: W, name: N, text: v}}); }};
const c = document.getElementById('c'); c.scrollTop = c.scrollHeight; setTimeout(() => location.reload(), 4000);
</script>"""

    # ---- shop ----
    def shop_auth(h: str):
        if not secrets.compare_digest(h.removeprefix("Bearer "), shop_token):
            raise HTTPException(401)

    @app.get("/shop/v1/orders")
    def by_phone(phone: str, authorization: str = Header("")):
        shop_auth(authorization)
        return {"orders": sorted((o for o in orders.values() if o["phone"] == phone), key=lambda o: o["placed"], reverse=True)}

    @app.get("/shop/v1/orders/{no}")
    def one(no: str, authorization: str = Header("")):
        shop_auth(authorization)
        if no not in orders:
            raise HTTPException(404)
        return orders[no]

    @app.post("/shop/v1/orders/{no}/returns")
    def make_return(no: str, body: dict, authorization: str = Header(""), idempotency_key: str = Header("")):
        shop_auth(authorization)
        if idempotency_key in returns:
            return returns[idempotency_key]
        r = {"id": f"R-{no}-{len(returns) + 1}", "order": no, "sku": body["sku"], "reason": body["reason"], "label_url": f"https://returns.example.com/label/{no}-{len(returns) + 1}"}
        returns[idempotency_key or secrets.token_hex(4)] = r
        return r

    @app.post("/_admin/ship")
    def ship(body: dict, x_admin: str = Header("")):
        admin(x_admin)
        o = orders[body["number"]]
        o["status"] = "shipped"
        raw = json.dumps({"event": "order.shipped", "order": o}).encode()
        sig = hmac.new(shop_secret.encode(), raw, hashlib.sha256).hexdigest()
        return {"status": httpx.post(shop_hook, content=raw, headers={"Content-Type": "application/json", "X-Shop-Signature": sig}, timeout=15).status_code}

    @app.get("/_admin/returns")
    def list_returns(x_admin: str = Header("")):
        admin(x_admin)
        return list(returns.values())

    return app
