"""Demo stand-in for the SMS gateway, with Twilio's request shapes, so nothing touches a real account:

- POST /2010-04-01/Accounts/{sid}/Messages.json   send a message (HTTP basic auth, form fields To/From/Body)
- inbound replies are POSTed to the app's webhook as form data signed with X-Twilio-Signature (HMAC-SHA1)
- /phone/{number}        a phone-screen view of one guest's conversation, for the demo
- /_admin/*              token-protected: read messages, make a guest reply, simulate outages
"""
import base64, hashlib, hmac, html, os, secrets, time

import httpx
from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse


def twilio_signature(token: str, url: str, params: dict) -> str:
    data = url + "".join(k + params[k] for k in sorted(params))
    return base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()


def create_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)
    sid, token, admin_token = os.environ["SMS_ACCOUNT_SID"], os.environ["SMS_AUTH_TOKEN"], os.environ["FAKES_ADMIN_TOKEN"]
    webhook = os.environ["SMS_WEBHOOK_URL"]          # where the gateway delivers inbound SMS (server to server)
    signed_url = os.environ.get("SMS_WEBHOOK_PUBLIC_URL", webhook)  # the URL configured in the console, used in the signature
    sender = os.environ.get("SMS_FROM", "+4972100000")
    messages: list[dict] = []
    chaos = {"fail": 0}

    def admin(x_admin: str):
        if not secrets.compare_digest(x_admin, admin_token):
            raise HTTPException(403)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.post("/2010-04-01/Accounts/{account}/Messages.json")
    def send(request: Request, account: str, To: str = Form(), From: str = Form(), Body: str = Form()):
        auth = request.headers.get("authorization", "")
        if account != sid or not secrets.compare_digest(auth, "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode()):
            raise HTTPException(401, {"code": 20003, "message": "Authenticate"})
        if chaos["fail"] > 0:
            chaos["fail"] -= 1
            raise HTTPException(503, {"message": "Service unavailable"})
        if not To.startswith("+") or len(To) < 9:
            return HTMLResponse('{"code": 21211, "message": "The \'To\' number is not a valid phone number."}', 400, media_type="application/json")
        m = {"sid": "SM" + secrets.token_hex(16), "to": To, "from": From, "body": Body, "direction": "outbound", "at": time.time()}
        messages.append(m)
        return {"sid": m["sid"], "status": "queued", "to": To, "body": Body}

    @app.get("/_admin/messages")
    def list_messages(to: str = "", x_admin: str = Header("")):
        admin(x_admin)
        return [m for m in messages if not to or to in (m["to"], m["from"])]

    @app.post("/_admin/reply")
    def reply(body: dict, x_admin: str = Header("")):
        """A guest texts back: deliver it to the app's webhook exactly like the real gateway would."""
        admin(x_admin)
        params = {"MessageSid": "SM" + secrets.token_hex(16), "AccountSid": sid, "From": body["from"], "To": sender, "Body": body["body"]}
        messages.append({"sid": params["MessageSid"], "to": sender, "from": body["from"], "body": body["body"], "direction": "inbound", "at": time.time()})
        sig = body.get("signature") or twilio_signature(token, signed_url, params)
        r = httpx.post(webhook, data=params, headers={"X-Twilio-Signature": sig}, timeout=15)
        return {"status": r.status_code, "sid": params["MessageSid"]}

    @app.post("/_admin/fail")
    def fail(body: dict, x_admin: str = Header("")):
        admin(x_admin)
        chaos["fail"] = int(body.get("count", 0))
        return chaos

    @app.get("/phone/{number}", response_class=HTMLResponse)
    def phone(number: str, request: Request):
        if not secrets.compare_digest(request.query_params.get("token", ""), admin_token):
            raise HTTPException(403)
        thread = [m for m in messages if number in (m["to"], m["from"])]
        bubbles = "".join(f'<div class="b {"in" if m["direction"] == "outbound" else "out"}">{html.escape(m["body"])}</div>' for m in thread)
        return f"""<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=2><title>{html.escape(number)}</title>
<style>body{{margin:0;background:#e5e7eb;font:15px/1.4 system-ui;display:flex;justify-content:center;padding:24px}}
.p{{width:360px;min-height:640px;background:#fff;border-radius:36px;border:10px solid #111;padding:18px 14px;display:flex;flex-direction:column;gap:8px}}
.h{{text-align:center;color:#6b7280;font-size:13px;margin-bottom:8px}} .h b{{display:block;color:#111;font-size:16px}}
.b{{max-width:80%;padding:9px 12px;border-radius:18px}} .in{{background:#f3f4f6;align-self:flex-start}} .out{{background:#2563eb;color:#fff;align-self:flex-end}}</style>
<div class=p><div class=h><b>{html.escape(sender)}</b>Text message · {html.escape(number)}</div>{bubbles}</div>"""

    return app
