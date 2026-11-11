"""Shop and carrier webhooks, and the warehouse panel: the queue by state, holds to fix, the day's label batch."""
import base64, hashlib, hmac, json, os, secrets
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from . import db as store
from . import flow, rules

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else None
TABS = [("ready", "Ready"), ("held", "On hold"), ("labelled", "To print"), ("shipped", "Shipped"), ("exception", "Problems"), ("delivered", "Delivered")]
CARRIERS = {"dhl": "DHL", "dhl_express": "DHL Express", "dpd": "DPD"}


def create_app(db_path: str | None = None, secret: str | None = None, carrier=None) -> FastAPI:
    app = FastAPI(title="Shipflow", docs_url=None, redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=secret or os.environ["SECRET_KEY"], same_site="strict",
                       https_only=os.environ.get("COOKIE_SECURE", "false").lower() == "true")
    path = db_path or os.environ.get("DB_PATH", "shipflow.db")
    data = Path(os.environ.get("DATA_DIR", "data"))
    shop_secret, carrier_secret = os.environ["SHOP_WEBHOOK_SECRET"], os.environ["CARRIER_SECRET"]
    carrier = carrier or flow.Carrier()
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

    def page(request: Request, name: str, conn=None, **ctx):
        request.session.setdefault("csrf", secrets.token_urlsafe(24))
        if conn is not None:
            ctx["counts"] = {r[0]: r[1] for r in conn.execute("select status, count(*) from orders group by status")}
        return templates.TemplateResponse(request, name, {"csrf": request.session["csrf"], "carriers": CARRIERS, **ctx})

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.post("/webhooks/shop")
    async def shop_hook(request: Request, x_shopify_hmac_sha256: str = Header(""), x_shopify_webhook_id: str = Header(""), conn=Depends(get_db)):
        body = await request.body()
        if not hmac.compare_digest(base64.b64encode(hmac.new(shop_secret.encode(), body, hashlib.sha256).digest()).decode(), x_shopify_hmac_sha256):
            raise HTTPException(401, "bad signature")
        if x_shopify_webhook_id and not conn.execute("insert or ignore into webhook_ids values (?, ?)", ("shop:" + x_shopify_webhook_id, store.now())).rowcount:
            return {"duplicate": True}
        return {"status": flow.receive(conn, json.loads(body))}

    @app.post("/webhooks/carrier")
    async def carrier_hook(request: Request, sendcloud_signature: str = Header(""), conn=Depends(get_db)):
        body = await request.body()
        if not hmac.compare_digest(hmac.new(carrier_secret.encode(), body, hashlib.sha256).hexdigest(), sendcloud_signature):
            raise HTTPException(401, "bad signature")
        ev = json.loads(body)
        if ev.get("action") != "parcel_status_changed":
            return {"ignored": ev.get("action")}
        p = ev["parcel"]
        return {"status": flow.track(conn, {"id": ev["id"], "parcel_id": p["id"], "status": p["status"]["message"], "message": p.get("note", "")})}

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
    def queue(request: Request, status: str = "ready", user=Depends(current_user), conn=Depends(get_db)):
        rows = conn.execute("""select o.*, p.tracking, p.tracking_url, p.last_event, p.printed_batch from orders o
                               left join parcels p on p.order_id = o.id and p.kind = 'outbound' where o.status = ? order by o.received_at""", (status,)).fetchall()
        return page(request, "queue.html", conn, user=user, rows=rows, status=status, tabs=TABS, msg=request.query_params.get("msg"),
                    batches=conn.execute("select * from batches order by id desc limit 5").fetchall())

    @app.post("/labels", dependencies=[Depends(check_csrf)])
    async def labels(user=Depends(current_user), conn=Depends(get_db)):
        ids = [r[0] for r in conn.execute("select id from orders where status = 'ready' order by received_at")]
        results = [await run_in_threadpool(flow.label, conn, carrier, i, data / "labels") for i in ids]
        made, held = results.count("labelled"), results.count("held")
        return RedirectResponse("/?status=labelled&msg=" + quote(f"{made} label(s) created" + (f", {held} refused by the carrier (now on hold)" if held else "") + "."), 303)

    @app.post("/batches", dependencies=[Depends(check_csrf)])
    def print_batch(user=Depends(current_user), conn=Depends(get_db)):
        b = flow.batch(conn, user["name"], data / "batches")
        if not b:
            return RedirectResponse("/?status=labelled&msg=" + quote("Nothing new to print."), 303)
        return RedirectResponse(f"/?status=labelled&msg=" + quote(f"Batch {b['id']}: {b['parcels']} labels in one PDF. They're marked as shipped in the shop once printed.") + f"&batch={b['id']}", 303)

    @app.get("/batches/{bid}.pdf")
    def batch_pdf(bid: int, user=Depends(current_user), conn=Depends(get_db)):
        b = conn.execute("select * from batches where id = ?", (bid,)).fetchone()
        if not b:
            raise HTTPException(404)
        return FileResponse(b["path"], media_type="application/pdf", filename=f"labels-batch-{bid}.pdf")

    @app.get("/orders/{oid}", response_class=HTMLResponse)
    def order(request: Request, oid: str, user=Depends(current_user), conn=Depends(get_db)):
        o = conn.execute("select * from orders where id = ?", (oid,)).fetchone()
        if not o:
            raise HTTPException(404)
        parcels = conn.execute("select * from parcels where order_id = ?", (oid,)).fetchall()
        return page(request, "order.html", conn, user=user, o=o, a=json.loads(o["address"]), lines=json.loads(o["lines"]), parcels=parcels,
                    events=conn.execute("select e.* from events e join parcels p on p.id = e.parcel_id where p.order_id = ? order by e.id", (oid,)).fetchall(),
                    log=conn.execute("select * from audit where order_id = ? order by id", (oid,)).fetchall(), error=request.query_params.get("error"))

    @app.post("/orders/{oid}/address", dependencies=[Depends(check_csrf)])
    def fix(oid: str, street: str = Form(""), postcode: str = Form(""), city: str = Form(""), phone: str = Form(""), user=Depends(current_user), conn=Depends(get_db)):
        try:
            status = flow.fix(conn, oid, {"street": street, "postcode": postcode, "city": city, "phone": phone}, user["name"])
        except ValueError as e:
            return RedirectResponse(f"/orders/{oid}?error=" + quote(str(e)), 303)
        return RedirectResponse(f"/orders/{oid}" if status == "held" else "/?status=ready&msg=" + quote("Fixed: the order is ready for a label."), 303)

    @app.get("/rules", response_class=HTMLResponse)
    def rules_page(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        cat = conn.execute("select * from products order by sku").fetchall()
        return page(request, "rules.html", conn, user=user, products=cat, max_kg=rules.MAX_PARCEL_G / 1000)

    return app
