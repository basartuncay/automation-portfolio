"""Panel and inbound webhooks: connect the CRM over OAuth, watch the sync, resolve conflicts, replay dead letters."""
import base64, hashlib, hmac, json, os, secrets
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import db as store
from .clients import CRM, Permanent, Retryable, Shop
from .engine import FIELDS, resolve
from .worker import backfill, enqueue

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else None


def verify_shopify(body: bytes, signature: str, secret: str) -> bool:
    expected = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(expected, signature or "")


def create_app(db_path: str | None = None, secret: str | None = None, http: httpx.Client | None = None) -> FastAPI:
    app = FastAPI(title="Sync Hub", docs_url=None, redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=secret or os.environ["SECRET_KEY"], same_site="lax",  # lax: the OAuth callback is a cross-site redirect
                       https_only=os.environ.get("COOKIE_SECURE", "false").lower() == "true")
    path = db_path or os.environ.get("DB_PATH", "hub.db")
    http = http or httpx.Client(timeout=20)
    webhook_secret = os.environ["SHOP_WEBHOOK_SECRET"]
    public_url = os.environ.get("PUBLIC_URL", "http://127.0.0.1:8094").rstrip("/")
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

    def counts(conn) -> dict:
        one = lambda sql: conn.execute(sql).fetchone()[0]
        return {"links": one("select count(*) from links"), "queued": one("select count(*) from events where status = 'queued'"),
                "dead": one("select count(*) from events where status = 'dead'"), "conflicts": one("select count(*) from conflicts where status = 'open'"),
                "done_today": one("select count(*) from events where status = 'done' and done_at >= date('now')")}

    def page(request: Request, name: str, conn=None, **ctx):
        request.session.setdefault("csrf", secrets.token_urlsafe(24))
        if conn is not None:
            ctx["n"] = counts(conn)
        return templates.TemplateResponse(request, name, {"csrf": request.session["csrf"], **ctx})

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # ---- inbound webhooks: verified, de-duplicated, queued, answered fast ----
    @app.post("/webhooks/shop")
    async def shop_webhook(request: Request, x_shopify_hmac_sha256: str = Header(""), x_shopify_webhook_id: str = Header(""),
                           x_shopify_topic: str = Header("")):
        body = await request.body()
        if not verify_shopify(body, x_shopify_hmac_sha256, webhook_secret):
            raise HTTPException(401, "bad signature")
        if not x_shopify_topic.startswith("customers/"):
            return {"ignored": x_shopify_topic}
        c = json.loads(body)
        conn = store.connect(path)
        try:
            fresh = enqueue(conn, "shop", f"shop:{x_shopify_webhook_id}", f"shop:{c['id']}", x_shopify_topic, c)
        finally:
            conn.close()
        return {"queued": fresh}

    # ---- auth ----
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

    # ---- OAuth connect ----
    @app.post("/connect/crm", dependencies=[Depends(check_csrf)])
    def connect_crm(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        state = secrets.token_urlsafe(16)
        request.session["oauth_state"] = state
        return RedirectResponse(CRM(conn, http).authorize_url(f"{public_url}/oauth/crm/callback", state), 303)

    @app.get("/oauth/crm/callback")
    def crm_callback(request: Request, code: str = "", state: str = "", user=Depends(current_user), conn=Depends(get_db)):
        expected = request.session.pop("oauth_state", None)
        if not expected or not secrets.compare_digest(state, expected):
            raise HTTPException(400, "OAuth state mismatch: start the connection again")
        try:
            CRM(conn, http).exchange(code, f"{public_url}/oauth/crm/callback")
        except (Retryable, Permanent) as e:
            return RedirectResponse(f"/?flash=CRM+connection+failed:+{str(e)[:80]}", 303)
        return RedirectResponse("/?flash=CRM+connected.+Run+a+full+sync+to+link+existing+records.", 303)

    # ---- pages ----
    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        crm_row = conn.execute("select name, status, connected_at, cursor, last_error from connections where name = 'crm'").fetchone()
        recent = conn.execute("select l.*, k.email from log l left join links k on k.id = l.link_id order by l.id desc limit 12").fetchall()
        return page(request, "overview.html", conn, user=user, crm=crm_row, recent=recent, active="overview",
                    flash=request.query_params.get("flash"))

    @app.post("/backfill", dependencies=[Depends(check_csrf)])
    def run_backfill(user=Depends(current_user), conn=Depends(get_db)):
        try:
            n = backfill(conn, CRM(conn, http), Shop(http))
        except (Retryable, Permanent) as e:
            return RedirectResponse(f"/?flash=Full+sync+failed:+{str(e)[:80]}", 303)
        return RedirectResponse(f"/?flash=Queued+{n}+records+for+a+full+sync", 303)

    @app.get("/log", response_class=HTMLResponse)
    def sync_log(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        rows = conn.execute("select l.*, k.email from log l left join links k on k.id = l.link_id order by l.id desc limit 300").fetchall()
        return page(request, "log.html", conn, user=user, rows=rows, active="log")

    @app.get("/conflicts", response_class=HTMLResponse)
    def conflicts(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        rows = conn.execute("select c.*, k.email from conflicts c join links k on k.id = c.link_id order by c.status = 'open' desc, c.id desc").fetchall()
        return page(request, "conflicts.html", conn, user=user, rows=rows, active="conflicts")

    @app.post("/conflicts/{cid}", dependencies=[Depends(check_csrf)])
    def resolve_conflict(cid: int, winner: str = Form(), user=Depends(current_user), conn=Depends(get_db)):
        if winner not in ("shop", "crm"):
            raise HTTPException(422)
        try:
            resolve(conn, CRM(conn, http), Shop(http), cid, winner, user["name"])
        except (Retryable, Permanent) as e:
            conn.rollback()
            return RedirectResponse(f"/conflicts?flash=Not+resolved:+{str(e)[:80]}", 303)
        return RedirectResponse("/conflicts", 303)

    @app.get("/dead", response_class=HTMLResponse)
    def dead(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        rows = conn.execute("select * from events where status = 'dead' order by id desc").fetchall()
        return page(request, "dead.html", conn, user=user, rows=rows, active="dead")

    @app.post("/dead/replay", dependencies=[Depends(check_csrf)])
    def replay(event_id: str = Form("all"), user=Depends(current_user), conn=Depends(get_db)):
        """Put dead letters back in the queue with a fresh retry budget (after fixing the cause)."""
        where, args = ("status = 'dead'", ()) if event_id == "all" else ("status = 'dead' and id = ?", (int(event_id),))
        n = conn.execute(f"update events set status = 'queued', attempts = 0, next_at = 0, last_error = null where {where}", args).rowcount
        conn.commit()
        return RedirectResponse(f"/dead?flash=Replaying+{n}+event(s)", 303)

    @app.get("/mapping", response_class=HTMLResponse)
    def mapping(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        return page(request, "mapping.html", conn, user=user, fields=FIELDS, active="mapping")

    return app
