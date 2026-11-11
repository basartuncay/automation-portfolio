"""Public pages (enquiry form, proposal with signing, signed PDF), the Stripe webhook, and the studio's panel."""
import hashlib, json, os, secrets, time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import db as store
from . import flow
from .db import money
from .services import Stripe, verify_stripe

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else None
templates.env.filters["money"] = money
STAGES = [("enquiry", "Enquiry"), ("proposal_sent", "Proposal sent"), ("viewed", "Viewed"), ("signed", "Signed"), ("paid", "Paid, setting up"), ("onboarded", "Onboarded")]


def create_app(db_path: str | None = None, secret: str | None = None, stripe=None) -> FastAPI:
    app = FastAPI(title="Kickoff", docs_url=None, redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=secret or os.environ["SECRET_KEY"], same_site="lax",  # lax: clients arrive from email links
                       https_only=os.environ.get("COOKIE_SECURE", "false").lower() == "true")
    path = db_path or os.environ.get("DB_PATH", "kickoff.db")
    contracts = Path(os.environ.get("CONTRACTS_DIR", "contracts"))
    whsec = os.environ["STRIPE_WEBHOOK_SECRET"]
    stripe = stripe or Stripe()
    hits: dict[str, deque] = defaultdict(deque)
    with store.connect(path) as conn:
        if os.environ.get("DEMO_SEED", "false").lower() == "true":
            store.seed_user(conn)

    def get_db():
        conn = store.connect(path)
        try:
            yield conn
        finally:
            conn.close()

    def current_user(request: Request, conn=Depends(get_db)):
        uid = request.session.get("uid")
        user = uid and conn.execute("select * from users where id = ?", (uid,)).fetchone()
        if not user:
            raise HTTPException(303, headers={"Location": "/login"})
        return user

    def check_csrf(request: Request, csrf: str = Form("")):
        if not csrf or not secrets.compare_digest(csrf, request.session.get("csrf", "")):
            raise HTTPException(400, "Form expired, go back and try again")

    def page(request: Request, name: str, **ctx):
        request.session.setdefault("csrf", secrets.token_urlsafe(24))
        return templates.TemplateResponse(request, name, {"csrf": request.session["csrf"], **ctx})

    def client_ip(request: Request) -> str:
        return request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0].strip()

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # ---------------------------------------------------------------- public
    @app.get("/", response_class=HTMLResponse)
    def enquiry_form(request: Request, conn=Depends(get_db)):
        pkgs = conn.execute("select * from packages order by id").fetchall()
        return page(request, "enquire.html", packages=pkgs, error=None, form={})

    @app.post("/enquire", response_class=HTMLResponse, dependencies=[Depends(check_csrf)])
    async def enquire(request: Request, conn=Depends(get_db)):
        form = await request.form()
        if form.get("website"):  # honeypot
            return page(request, "thanks.html")
        q, t = hits[client_ip(request)], time.time()
        while q and q[0] < t - 3600:
            q.popleft()
        if len(q) >= 5:
            raise HTTPException(429, "Too many enquiries from this connection. Please email us instead.")
        try:
            flow.enquire(conn, name=str(form.get("name", "")), email_=str(form.get("email", "")), company=str(form.get("company", "")),
                         brief=str(form.get("brief", "")), package_id=int(form.get("package") or 0), options=form.getlist("options"))
        except flow.Invalid as e:
            return page(request, "enquire.html", packages=conn.execute("select * from packages order by id").fetchall(), error=str(e), form=dict(form))
        q.append(t)
        return page(request, "thanks.html")

    def proposal_ctx(conn, p, c):
        pkg = conn.execute("select * from packages where id = ?", (c["package_id"],)).fetchone()
        text = flow.contract_text(p, c, pkg)
        pay = conn.execute("select * from payments where proposal_id = ? order by id desc limit 1", (p["id"],)).fetchone()
        return {"p": p, "c": c, "pkg": pkg, "items": json.loads(p["items"]), "sha": hashlib.sha256(text.encode()).hexdigest(), "text": text,
                "deposit": p["total_cents"] * p["deposit_pct"] // 100, "pay": pay, "sig": json.loads(p["signature"]) if p["signature"] else None}

    @app.get("/p/{token}", response_class=HTMLResponse)
    def proposal(request: Request, token: str, conn=Depends(get_db)):
        p, c = flow.view(conn, token)
        if not p:
            raise HTTPException(404, "This link isn't valid any more.")
        return page(request, "proposal.html", error=request.query_params.get("error"), paid=request.query_params.get("paid"), **proposal_ctx(conn, p, c))

    @app.post("/p/{token}/sign", dependencies=[Depends(check_csrf)])
    def sign(request: Request, token: str, typed_name: str = Form(""), agree: str = Form(""), sha256: str = Form(""), conn=Depends(get_db)):
        try:
            r = flow.sign(conn, token, typed_name=typed_name, agreed=bool(agree), shown_sha256=sha256, ip=client_ip(request),
                          user_agent=request.headers.get("user-agent", ""), stripe=stripe, contracts_dir=contracts)
        except flow.Invalid as e:
            return RedirectResponse(f"/p/{token}?error=" + quote(str(e)), 303)
        return RedirectResponse(r["pay_url"] or f"/p/{token}", 303)

    @app.post("/p/{token}/pay")
    def pay_again(token: str, conn=Depends(get_db)):
        p = conn.execute("select * from proposals where token = ? and status = 'signed'", (token,)).fetchone()
        if not p:
            raise HTTPException(404)
        return RedirectResponse(flow.start_payment(conn, p["id"], stripe), 303)

    @app.get("/p/{token}/pdf")
    def signed_pdf(token: str, conn=Depends(get_db)):
        p = conn.execute("select pdf from proposals where token = ? and status = 'signed'", (token,)).fetchone()
        if not p or not Path(p["pdf"]).exists():
            raise HTTPException(404)
        return FileResponse(p["pdf"], media_type="application/pdf", filename="agreement-signed.pdf")

    @app.post("/webhooks/stripe")
    async def stripe_webhook(request: Request, stripe_signature: str = Header(""), conn=Depends(get_db)):
        body = await request.body()
        if not verify_stripe(body, stripe_signature, whsec):
            raise HTTPException(400, "bad signature")
        return {"result": flow.handle_stripe_event(conn, json.loads(body))}

    # ---------------------------------------------------------------- studio
    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return page(request, "login.html", error=None)

    @app.post("/login", dependencies=[Depends(check_csrf)])
    def login(request: Request, email: str = Form(), password: str = Form(), conn=Depends(get_db)):
        user = conn.execute("select * from users where email = ?", (email.strip().lower(),)).fetchone()
        if not user or not store.check_pw(password, user["pw_hash"]):
            return page(request, "login.html", error="Wrong email or password")
        request.session.clear()
        request.session.update(uid=user["id"], csrf=secrets.token_urlsafe(24))
        return RedirectResponse("/board", 303)

    @app.post("/logout", dependencies=[Depends(check_csrf)])
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", 303)

    @app.get("/board", response_class=HTMLResponse)
    def board(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        rows = conn.execute("""select c.*, p.total_cents, p.version, pk.name package from clients c join packages pk on pk.id = c.package_id
                               left join proposals p on p.id = (select max(id) from proposals where client_id = c.id) where c.stage != 'lost' order by c.id desc""").fetchall()
        cols = {k: [r for r in rows if r["stage"] == k] for k, _ in STAGES}
        won = conn.execute("select coalesce(sum(amount_cents), 0) from payments where status = 'paid'").fetchone()[0]
        pipeline = sum(r["total_cents"] or 0 for r in rows if r["stage"] in ("proposal_sent", "viewed"))
        failed = conn.execute("select count(*) from steps where status = 'failed'").fetchone()[0]
        return page(request, "board.html", user=user, active="board", stages=STAGES, cols=cols, won=won, pipeline=pipeline, failed=failed)

    @app.get("/clients/{cid}", response_class=HTMLResponse)
    def client(request: Request, cid: int, user=Depends(current_user), conn=Depends(get_db)):
        c = conn.execute("select * from clients where id = ?", (cid,)).fetchone()
        if not c:
            raise HTTPException(404)
        p = conn.execute("select * from proposals where client_id = ? order by id desc limit 1", (cid,)).fetchone()
        return page(request, "client.html", user=user, active="board", **proposal_ctx(conn, p, c),
                    older=conn.execute("select * from proposals where client_id = ? and id != ? order by id desc", (cid, p["id"])).fetchall(),
                    steps=conn.execute("select * from steps where client_id = ? order by position", (cid,)).fetchall(),
                    emails=conn.execute("select * from outbox where client_id = ? order by id", (cid,)).fetchall(),
                    events=conn.execute("select * from events where client_id = ? order by id", (cid,)).fetchall(),
                    msg=request.query_params.get("msg"), err=request.query_params.get("err"))

    def back(cid: int, msg: str = "", err: str = "") -> RedirectResponse:
        return RedirectResponse(f"/clients/{cid}?" + (f"msg={quote(msg)}" if msg else f"err={quote(err)}"), 303)

    @app.post("/clients/{cid}/proposal", dependencies=[Depends(check_csrf)])
    async def edit(request: Request, cid: int, user=Depends(current_user), conn=Depends(get_db)):
        form = await request.form()
        p = conn.execute("select * from proposals where client_id = ? order by id desc limit 1", (cid,)).fetchone()
        try:
            items = [{"name": str(n)[:200], "detail": str(d)[:300], "cents": round(float(str(a).replace(",", ".") or 0) * 100)}
                     for n, d, a in zip(form.getlist("item_name"), form.getlist("item_detail"), form.getlist("item_amount"))]
            flow.edit_proposal(conn, p["id"], items, int(form.get("deposit_pct") or 0), str(form.get("intro", "")))
        except (flow.Invalid, ValueError) as e:
            return back(cid, err=str(e) if isinstance(e, flow.Invalid) else "Amounts must be numbers, like 1200 or 1200.50.")
        return back(cid, msg="Draft saved.")

    @app.post("/clients/{cid}/send", dependencies=[Depends(check_csrf)])
    def send(cid: int, user=Depends(current_user), conn=Depends(get_db)):
        p = conn.execute("select * from proposals where client_id = ? order by id desc limit 1", (cid,)).fetchone()
        try:
            flow.send_proposal(conn, p["id"], user["name"])
        except flow.Invalid as e:
            return back(cid, err=str(e))
        return back(cid, msg="Sent. You'll see here when it's opened and signed.")

    @app.post("/clients/{cid}/withdraw", dependencies=[Depends(check_csrf)])
    def withdraw(cid: int, user=Depends(current_user), conn=Depends(get_db)):
        p = conn.execute("select * from proposals where client_id = ? order by id desc limit 1", (cid,)).fetchone()
        try:
            flow.withdraw(conn, p["id"], user["name"])
        except flow.Invalid as e:
            return back(cid, err=str(e))
        return back(cid, msg="Withdrawn. The old link no longer works; edit the new draft and send it.")

    @app.post("/clients/{cid}/lost", dependencies=[Depends(check_csrf)])
    def lost(cid: int, user=Depends(current_user), conn=Depends(get_db)):
        conn.execute("update clients set stage = 'lost' where id = ? and stage in ('enquiry', 'proposal_sent', 'viewed')", (cid,))
        flow.cancel_reminders(conn, cid, "not_viewed", "not_signed", "not_paid")
        store.event(conn, cid, "lost", f"marked by {user['name']}")
        return RedirectResponse("/board", 303)

    @app.post("/steps/{sid}/retry", dependencies=[Depends(check_csrf)])
    def retry(sid: int, user=Depends(current_user), conn=Depends(get_db)):
        s = conn.execute("select client_id from steps where id = ?", (sid,)).fetchone()
        flow.retry_step(conn, sid)
        return back(s["client_id"], msg="Retrying in a few seconds.")

    return app
