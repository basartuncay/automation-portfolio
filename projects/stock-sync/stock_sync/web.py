"""Panel and inbound webhooks: stock across channels, the ledger per SKU, orders, exceptions, the sync log and
reconciliation. Shopify and warehouse webhooks are verified, de-duplicated and booked straight into the ledger."""
import base64, hashlib, hmac, json, os, secrets
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import db as store
from .ledger import adjust, available, parts, retry_order, targets
from .sync import handle_shopify, handle_wms, reconcile

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else None


def create_app(db_path: str | None = None, secret: str | None = None, clients=None, wms=None) -> FastAPI:
    app = FastAPI(title="Stock Sync", docs_url=None, redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=secret or os.environ["SECRET_KEY"], same_site="strict",
                       https_only=os.environ.get("COOKIE_SECURE", "false").lower() == "true")
    path = db_path or os.environ.get("DB_PATH", "stock.db")
    shop_secret, wms_secret = os.environ["SHOPIFY_WEBHOOK_SECRET"], os.environ["WMS_WEBHOOK_SECRET"]
    with store.connect(path) as conn:
        store.seed_catalog(conn)
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

    def page(request: Request, name: str, conn=None, **ctx):
        request.session.setdefault("csrf", secrets.token_urlsafe(24))
        if conn is not None:
            ctx["n"] = {"exceptions": conn.execute("select count(*) from exceptions where status = 'open'").fetchone()[0]}
        return templates.TemplateResponse(request, name, {"csrf": request.session["csrf"], **ctx})

    def seen(conn, wid: str) -> bool:
        return not conn.execute("insert or ignore into webhook_ids values (?, ?)", (wid, store.now())).rowcount

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.post("/webhooks/shopify")
    async def shopify_webhook(request: Request, x_shopify_hmac_sha256: str = Header(""), x_shopify_topic: str = Header(""),
                              x_shopify_webhook_id: str = Header(""), conn=Depends(get_db)):
        body = await request.body()
        expected = base64.b64encode(hmac.new(shop_secret.encode(), body, hashlib.sha256).digest()).decode()
        if not hmac.compare_digest(expected, x_shopify_hmac_sha256):
            raise HTTPException(401, "bad signature")
        if x_shopify_webhook_id and seen(conn, "shopify:" + x_shopify_webhook_id):
            return {"duplicate": True}
        return {"result": handle_shopify(conn, x_shopify_topic, json.loads(body))}

    @app.post("/webhooks/wms")
    async def wms_webhook(request: Request, x_wms_signature: str = Header(""), conn=Depends(get_db)):
        body = await request.body()
        if not hmac.compare_digest(hmac.new(wms_secret.encode(), body, hashlib.sha256).hexdigest(), x_wms_signature):
            raise HTTPException(401, "bad signature")
        return {"result": handle_wms(conn, json.loads(body))}

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

    @app.get("/", response_class=HTMLResponse)
    def stock(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        want = targets(conn)
        listings = {}
        for l in conn.execute("select * from listings order by channel"):
            listings.setdefault(l["sku"], {})[l["channel"]] = {**dict(l), "target": want.get(l["id"])}
        rows = []
        for s in conn.execute("select * from skus order by is_bundle, sku").fetchall():
            avail = available(conn, s["sku"])
            limit = None
            if s["is_bundle"]:
                limit = min(parts(conn, s["sku"]), key=lambda p: available(conn, p[0]) // p[1])[0]
            rows.append({"s": s, "available": avail, "listings": listings.get(s["sku"], {}), "limit": limit, "low": avail <= s["low_at"]})
        log = conn.execute("select * from sync_log order by id desc limit 12").fetchall()
        open_orders = conn.execute("select count(*) from orders where status = 'reserved'").fetchone()[0]
        return page(request, "stock.html", conn, user=user, active="stock", rows=rows, log=log, open_orders=open_orders, channels=["shopify", "amazon"])

    @app.get("/sku/{sku}", response_class=HTMLResponse)
    def sku_detail(request: Request, sku: str, user=Depends(current_user), conn=Depends(get_db)):
        s = conn.execute("select * from skus where sku = ?", (sku,)).fetchone()
        if not s:
            raise HTTPException(404)
        want = targets(conn)
        return page(request, "sku.html", conn, user=user, active="stock", s=s, available=available(conn, sku),
                    parts=conn.execute("select bp.*, s.name from bundle_parts bp join skus s on s.sku = bp.part where bundle = ?", (sku,)).fetchall(),
                    used_in=conn.execute("select bundle, qty from bundle_parts where part = ?", (sku,)).fetchall(),
                    listings=[{**dict(l), "target": want.get(l["id"])} for l in conn.execute("select * from listings where sku = ?", (sku,))],
                    moves=conn.execute("select * from movements where sku = ? order by id desc limit 100", (sku,)).fetchall(),
                    error=request.query_params.get("error"), msg=request.query_params.get("msg"))

    @app.post("/sku/{sku}/adjust", dependencies=[Depends(check_csrf)])
    def sku_adjust(sku: str, delta: int = Form(), reason: str = Form(), user=Depends(current_user), conn=Depends(get_db)):
        if not reason.strip():
            return RedirectResponse(f"/sku/{sku}?error=" + quote("Say why: the ledger keeps a reason for every change."), 303)
        try:
            adjust(conn, sku, delta, f"manual:{secrets.token_hex(6)}", f"{reason.strip()} ({user['name']})")
        except ValueError as e:
            return RedirectResponse(f"/sku/{sku}?error=" + quote(str(e)), 303)
        return RedirectResponse(f"/sku/{sku}?msg=" + quote(f"Adjusted by {delta:+d}."), 303)

    @app.post("/listings/{lid}", dependencies=[Depends(check_csrf)])
    def listing_update(lid: int, buffer: int = Form(0), max_qty: str = Form(""), enabled: str = Form(""), user=Depends(current_user), conn=Depends(get_db)):
        l = conn.execute("select * from listings where id = ?", (lid,)).fetchone()
        if not l:
            raise HTTPException(404)
        conn.execute("update listings set buffer = ?, max_qty = ?, enabled = ? where id = ?",
                     (max(0, buffer), int(max_qty) if max_qty.strip().isdigit() else None, 1 if enabled else 0, lid))
        return RedirectResponse(f"/sku/{l['sku']}?msg=" + quote(f"{l['channel']} listing saved, it will be pushed within seconds."), 303)

    @app.get("/orders", response_class=HTMLResponse)
    def orders(request: Request, status: str = "", user=Depends(current_user), conn=Depends(get_db)):
        sql = "select o.*, group_concat(l.qty || ' × ' || l.sku, ', ') lines from orders o join order_lines l on l.order_id = o.id"
        args = []
        if status:
            sql, args = sql + " where o.status = ?", [status]
        rows = conn.execute(sql + " group by o.id order by o.id desc limit 200", args).fetchall()
        counts = {r[0]: r[1] for r in conn.execute("select status, count(*) from orders group by status")}
        return page(request, "orders.html", conn, user=user, active="orders", rows=rows, status=status, counts=counts)

    @app.post("/orders/{oid}/retry", dependencies=[Depends(check_csrf)])
    def order_retry(oid: int, user=Depends(current_user), conn=Depends(get_db)):
        r = retry_order(conn, oid, user["name"])
        return RedirectResponse("/exceptions?msg=" + quote("Reserved." if r == "reserved" else "Still not enough stock."), 303)

    @app.get("/exceptions", response_class=HTMLResponse)
    def exceptions(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        return page(request, "exceptions.html", conn, user=user, active="exceptions", msg=request.query_params.get("msg"),
                    rows=conn.execute("select e.*, o.channel, o.external_id, o.status ostatus from exceptions e left join orders o on o.id = e.order_id order by e.status = 'open' desc, e.id desc limit 100").fetchall())

    @app.post("/exceptions/{eid}/resolve", dependencies=[Depends(check_csrf)])
    def exception_resolve(eid: int, user=Depends(current_user), conn=Depends(get_db)):
        conn.execute("update exceptions set status = 'resolved', resolved_by = ?, resolved_at = ? where id = ?", (user["name"], store.now(), eid))
        return RedirectResponse("/exceptions", 303)

    @app.get("/log", response_class=HTMLResponse)
    def sync_log(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        return page(request, "log.html", conn, user=user, active="log", rows=conn.execute("select * from sync_log order by id desc limit 300").fetchall())

    @app.get("/reconcile", response_class=HTMLResponse)
    def recon(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        runs = conn.execute("select * from recon_runs order by id desc limit 20").fetchall()
        return page(request, "reconcile.html", conn, user=user, active="recon", runs=runs, latest=runs[0] if runs else None,
                    error=request.query_params.get("error"))

    @app.post("/reconcile", dependencies=[Depends(check_csrf)])
    def recon_now(user=Depends(current_user), conn=Depends(get_db)):
        from .channels import Amazon, Shopify, Warehouse
        try:
            reconcile(conn, wms or Warehouse(), clients or {"shopify": Shopify(), "amazon": Amazon()})
        except Exception as e:  # noqa: BLE001 - show the reason instead of a 500
            return RedirectResponse("/reconcile?error=" + quote(f"Reconciliation failed: {e}"), 303)
        return RedirectResponse("/reconcile", 303)

    return app
