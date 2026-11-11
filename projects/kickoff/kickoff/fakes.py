"""Demo stand-ins with the real request shapes, so nothing touches a real account or card:

- /stripe    POST /v1/checkout/sessions (form-encoded, basic auth with the secret key, Idempotency-Key), a hosted
             payment page, and checkout.session.completed webhooks signed like Stripe (Stripe-Signature: t=..,v1=..)
- /projects  a Linear/Asana-style API: projects with an external reference, tasks
- /storage   a Drive-style API: folders and sharing permissions
/_admin endpoints (token-protected) inject outages and read state.
"""
import hashlib, hmac, html, json, os, secrets, time

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse


def stripe_header(secret: str, payload: bytes, t: int | None = None) -> str:
    t = t or int(time.time())
    return f"t={t},v1={hmac.new(secret.encode(), f'{t}.'.encode() + payload, hashlib.sha256).hexdigest()}"


def create_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)
    e = os.environ
    key, whsec, admin_token = e["STRIPE_SECRET_KEY"], e["STRIPE_WEBHOOK_SECRET"], e["FAKES_ADMIN_TOKEN"]
    hook, public = e["STRIPE_WEBHOOK_URL"], e.get("FAKES_PUBLIC_URL", "http://127.0.0.1:8105").rstrip("/")
    ptoken, stoken = e["PROJECTS_TOKEN"], e["STORAGE_TOKEN"]
    sessions, by_key, events = {}, {}, []
    projects, tasks, files, perms = [], {}, [], {}
    chaos = {"projects_fail": 0}

    def admin(x):
        if not secrets.compare_digest(x, admin_token):
            raise HTTPException(403)

    def bearer(h: str, expected: str):
        if not secrets.compare_digest(h.removeprefix("Bearer "), expected):
            raise HTTPException(401)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # ---- Stripe ----
    @app.post("/stripe/v1/checkout/sessions")
    async def create_session(request: Request, idempotency_key: str = Header("")):
        import base64
        if request.headers.get("authorization") != "Basic " + base64.b64encode(f"{key}:".encode()).decode():
            raise HTTPException(401, {"error": {"type": "invalid_request_error", "message": "Invalid API Key provided"}})
        if idempotency_key and idempotency_key in by_key:
            return sessions[by_key[idempotency_key]]
        f = dict(await request.form())
        sid = "cs_test_" + secrets.token_hex(12)
        s = {"id": sid, "object": "checkout.session", "url": f"{public}/stripe/pay/{sid}", "amount_total": int(f["line_items[0][price_data][unit_amount]"]),
             "currency": f["line_items[0][price_data][currency]"], "customer_email": f.get("customer_email"), "client_reference_id": f.get("client_reference_id"),
             "metadata": {"proposal_id": f.get("metadata[proposal_id]")}, "payment_status": "unpaid", "status": "open",
             "success_url": f["success_url"], "description": f["line_items[0][price_data][product_data][name]"]}
        sessions[sid] = s
        if idempotency_key:
            by_key[idempotency_key] = sid
        return s

    def complete(sid: str) -> int:
        s = sessions[sid]
        s.update(payment_status="paid", status="complete")
        ev = {"id": "evt_" + secrets.token_hex(12), "type": "checkout.session.completed", "created": int(time.time()), "data": {"object": s}}
        events.append(ev)
        body = json.dumps(ev).encode()
        return httpx.post(hook, content=body, headers={"Content-Type": "application/json", "Stripe-Signature": stripe_header(whsec, body)}, timeout=15).status_code

    @app.get("/stripe/pay/{sid}", response_class=HTMLResponse)
    def pay_page(sid: str):
        s = sessions.get(sid) or {}
        if not s:
            raise HTTPException(404)
        return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Pay</title>
<style>body{{margin:0;font:16px/1.5 system-ui;background:#f6f9fc;color:#30313d}}.c{{max-width:420px;margin:10vh auto;background:#fff;border-radius:12px;padding:28px;box-shadow:0 2px 10px #0001}}
.amt{{font-size:32px;font-weight:700}} button{{width:100%;border:0;border-radius:8px;padding:13px;background:#635bff;color:#fff;font:600 16px system-ui;cursor:pointer}}
.t{{color:#6b7280;font-size:13px}}</style><div class=c><div class=t>Test mode · local stand-in</div><p>{html.escape(s['description'])}</p>
<div class=amt>€{s['amount_total'] / 100:,.2f}</div><p class=t>{html.escape(s['customer_email'] or '')}</p>
{'<p>Paid.</p>' if s['payment_status'] == 'paid' else f'<form method=post action="/stripe/pay/{sid}"><button>Pay €{s["amount_total"] / 100:,.2f}</button></form>'}</div>"""

    @app.post("/stripe/pay/{sid}")
    def pay(sid: str):
        if sid not in sessions:
            raise HTTPException(404)
        if sessions[sid]["payment_status"] != "paid":
            complete(sid)
        return RedirectResponse(sessions[sid]["success_url"], 303)

    @app.post("/_admin/stripe/replay")
    def replay(body: dict, x_admin: str = Header("")):
        """Send the last event again: as is (a retry), or with an old timestamp (a replay attack)."""
        admin(x_admin)
        raw = json.dumps(events[-1]).encode()
        t = int(time.time()) - (3600 if body.get("old") else 0)
        sig = stripe_header(whsec if not body.get("forged") else "wrong", raw, t)
        return {"status": httpx.post(hook, content=raw, headers={"Content-Type": "application/json", "Stripe-Signature": sig}, timeout=15).status_code}

    # ---- project tool ----
    @app.get("/projects/v1/projects")
    def find_projects(external_ref: str, authorization: str = Header("")):
        bearer(authorization, ptoken)
        return {"data": [p for p in projects if p["external_ref"] == external_ref]}

    @app.post("/projects/v1/projects")
    def create_project(body: dict, authorization: str = Header("")):
        bearer(authorization, ptoken)
        if chaos["projects_fail"] > 0:
            chaos["projects_fail"] -= 1
            raise HTTPException(503, "maintenance")
        p = {"id": f"prj_{len(projects) + 1}", "name": body["name"], "external_ref": body["external_ref"], "url": f"https://projects.example.com/p/{len(projects) + 1}"}
        projects.append(p)
        tasks[p["id"]] = []
        return p

    @app.get("/projects/v1/projects/{pid}/tasks")
    def list_tasks(pid: str, authorization: str = Header("")):
        bearer(authorization, ptoken)
        return {"data": tasks[pid]}

    @app.post("/projects/v1/projects/{pid}/tasks")
    def add_task(pid: str, body: dict, authorization: str = Header("")):
        bearer(authorization, ptoken)
        t = {"id": f"tsk_{sum(map(len, tasks.values())) + 1}", "title": body["title"]}
        tasks[pid].append(t)
        return t

    # ---- storage ----
    @app.get("/storage/v1/files")
    def find_files(q: str, authorization: str = Header("")):
        bearer(authorization, stoken)
        name = q.split("name = '", 1)[1].split("' and", 1)[0]
        return {"files": [f for f in files if f["name"] == name]}

    @app.post("/storage/v1/files")
    def create_file(body: dict, authorization: str = Header("")):
        bearer(authorization, stoken)
        f = {"id": f"fld_{len(files) + 1}", "name": body["name"], "mimeType": body["mimeType"], "webViewLink": f"https://drive.example.com/folders/{len(files) + 1}"}
        files.append(f)
        perms[f["id"]] = []
        return f

    @app.get("/storage/v1/files/{fid}/permissions")
    def list_perms(fid: str, authorization: str = Header("")):
        bearer(authorization, stoken)
        return {"permissions": perms[fid]}

    @app.post("/storage/v1/files/{fid}/permissions")
    def share(fid: str, body: dict, authorization: str = Header("")):
        bearer(authorization, stoken)
        perms[fid].append({"role": body["role"], "emailAddress": body["emailAddress"]})
        return perms[fid][-1]

    @app.post("/_admin/chaos")
    def set_chaos(body: dict, x_admin: str = Header("")):
        admin(x_admin)
        chaos.update({k: int(v) for k, v in body.items() if k in chaos})
        return chaos

    @app.get("/_admin/state")
    def state(x_admin: str = Header("")):
        admin(x_admin)
        return {"sessions": list(sessions.values()), "projects": projects, "tasks": tasks, "files": files, "permissions": perms}

    return app
