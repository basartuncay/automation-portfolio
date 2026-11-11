"""The product-facing API (usage events, entitlement check), Stripe webhooks, and the billing panel."""
import json, os, secrets
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from . import billing, db as store
from .db import euro
from .stripe_api import Stripe, StripeError, verify

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["euro"] = euro


def create_app(db_path: str | None = None, secret: str | None = None, stripe=None) -> FastAPI:
    app = FastAPI(title="Meter", docs_url=None, redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=secret or os.environ["SECRET_KEY"], same_site="strict",
                       https_only=os.environ.get("COOKIE_SECURE", "false").lower() == "true")
    path = db_path or os.environ.get("DB_PATH", "meter.db")
    whsec = os.environ["STRIPE_WEBHOOK_SECRET"]
    stripe = stripe or Stripe()
    with store.connect(path) as conn:
        if os.environ.get("DEMO_SEED", "false").lower() == "true":
            store.seed_user(conn)

    def get_db():
        conn = store.connect(path)
        try:
            yield conn
        finally:
            conn.close()

    def api_customer(authorization: str = Header(""), conn=Depends(get_db)):
        c = billing.customer_by_key(conn, authorization.removeprefix("Bearer ").strip())
        if not c:
            raise HTTPException(401, "invalid API key")
        return c

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

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # ---- for the product ----
    @app.post("/v1/events")
    async def events(request: Request, c=Depends(api_customer), conn=Depends(get_db)):
        if int(request.headers.get("content-length") or 0) > 512_000:
            return JSONResponse({"error": "batch too large (max 1,000 events, 500 KB)"}, 413)
        body = await request.json()
        batch = body.get("events") if isinstance(body, dict) else None
        if not isinstance(batch, list):
            return JSONResponse({"error": "send {\"events\": [...]}"}, 422)
        return billing.ingest(conn, c, batch)

    @app.get("/v1/entitlement")
    def entitlement(c=Depends(api_customer), conn=Depends(get_db)):
        return billing.entitlement(conn, c)

    @app.post("/webhooks/stripe")
    async def stripe_webhook(request: Request, stripe_signature: str = Header(""), conn=Depends(get_db)):
        body = await request.body()
        if not verify(body, stripe_signature, whsec):
            raise HTTPException(400, "bad signature")
        return {"result": await run_in_threadpool(billing.handle_event, conn, stripe, json.loads(body))}

    # ---- panel ----
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
        return RedirectResponse("/", 303)

    @app.post("/logout", dependencies=[Depends(check_csrf)])
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", 303)

    def summary(conn, c):
        plan = conn.execute("select * from plans where id = ?", (c["plan_id"],)).fetchone()
        used = billing.usage(conn, c)
        return {"c": c, "plan": plan, "used": used, "pct": min(100, round(100 * used / plan["included"])), "over": used > plan["included"],
                "projected": billing.projected_cents(plan, used), "pending": conn.execute("select name from plans where id = ?", (c["pending_plan_id"],)).fetchone() if c["pending_plan_id"] else None}

    @app.get("/", response_class=HTMLResponse)
    def customers(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        rows = [summary(conn, c) for c in conn.execute("select * from customers order by created_at").fetchall()]
        mrr = sum(r["projected"] for r in rows if r["c"]["status"] != "cancelled")
        return page(request, "customers.html", user=user, active="customers", rows=rows, mrr=mrr, plans=conn.execute("select * from plans").fetchall(),
                    new_key=request.session.pop("new_key", None), error=request.query_params.get("error"))

    @app.post("/customers", dependencies=[Depends(check_csrf)])
    async def new_customer(request: Request, name: str = Form(), email: str = Form(), plan: str = Form(), user=Depends(current_user), conn=Depends(get_db)):
        cid = "c_" + secrets.token_hex(4)
        try:
            key = await run_in_threadpool(billing.create_customer, conn, stripe, cid=cid, name=name.strip()[:80], email=email.strip()[:120], plan_id=plan)
        except StripeError as e:
            return RedirectResponse("/?error=" + quote(f"Stripe: {e}"), 303)
        request.session["new_key"] = {"customer": name, "key": key}
        return RedirectResponse("/", 303)

    @app.get("/customers/{cid}", response_class=HTMLResponse)
    def customer(request: Request, cid: str, user=Depends(current_user), conn=Depends(get_db)):
        c = conn.execute("select * from customers where id = ?", (cid,)).fetchone()
        if not c:
            raise HTTPException(404)
        days = conn.execute("select substr(ts, 1, 10) d, sum(quantity) q, sum(late) late from events where customer_id = ? and period_start = ? group by 1 order by 1",
                            (cid, c["period_start"])).fetchall()
        top = max([d["q"] for d in days] + [1])
        return page(request, "customer.html", user=user, active="customers", **summary(conn, c), days=days, top=top, ent=billing.entitlement(conn, c),
                    plans=conn.execute("select * from plans").fetchall(), msg=request.query_params.get("msg"), error=request.query_params.get("error"),
                    invoices=conn.execute("select * from invoices where customer_id = ? order by period_start desc", (cid,)).fetchall(),
                    log=conn.execute("select * from audit where customer_id = ? order by id desc limit 40", (cid,)).fetchall(),
                    unreported=conn.execute("select count(*) from events where customer_id = ? and reported = 0", (cid,)).fetchone()[0])

    @app.post("/customers/{cid}/plan", dependencies=[Depends(check_csrf)])
    async def plan(cid: str, plan: str = Form(), user=Depends(current_user), conn=Depends(get_db)):
        try:
            msg = await run_in_threadpool(billing.change_plan, conn, stripe, cid, plan, user["name"])
        except (ValueError, StripeError) as e:
            return RedirectResponse(f"/customers/{cid}?error=" + quote(str(e)), 303)
        return RedirectResponse(f"/customers/{cid}?msg=" + quote(msg), 303)

    @app.post("/customers/{cid}/cap", dependencies=[Depends(check_csrf)])
    def cap(cid: str, cap: str = Form(""), user=Depends(current_user), conn=Depends(get_db)):
        cents = round(float(cap.replace(",", ".")) * 100) if cap.strip() else None
        conn.execute("update customers set spend_cap_cents = ? where id = ?", (cents, cid))
        store.audit(conn, cid, user["name"], f"spend cap {'set to ' + euro(cents) if cents else 'removed'}")
        return RedirectResponse(f"/customers/{cid}?msg=" + quote("Spend cap saved."), 303)

    @app.get("/reconcile", response_class=HTMLResponse)
    async def recon(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        try:
            rows = await run_in_threadpool(billing.reconcile, conn, stripe)
            error = None
        except StripeError as e:
            rows, error = [], f"Stripe: {e}"
        return page(request, "reconcile.html", user=user, active="recon", rows=rows, error=error)

    @app.get("/emails", response_class=HTMLResponse)
    def emails(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        return page(request, "emails.html", user=user, active="emails", rows=conn.execute("select * from outbox order by id desc limit 100").fetchall())

    return app
