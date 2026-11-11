"""The WhatsApp webhook (verification handshake, signature check), the shop's events, and the team inbox."""
import hashlib, hmac, json, os, secrets, time
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from . import bot, db as store
from .clients import Shop, WhatsApp, template

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else None


def create_app(db_path: str | None = None, secret: str | None = None, wa=None, shop=None) -> FastAPI:
    app = FastAPI(title="Chatline", docs_url=None, redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=secret or os.environ["SECRET_KEY"], same_site="strict",
                       https_only=os.environ.get("COOKIE_SECURE", "false").lower() == "true")
    path = db_path or os.environ.get("DB_PATH", "chatline.db")
    app_secret, verify_token, shop_secret = os.environ["WA_APP_SECRET"], os.environ["WA_VERIFY_TOKEN"], os.environ["SHOP_WEBHOOK_SECRET"]
    wa, shop = wa or WhatsApp(), shop or Shop()
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
            ctx["n"] = {"waiting": conn.execute("select count(*) from conversations where mode = 'human' and unread > 0").fetchone()[0]}
        return templates.TemplateResponse(request, name, {"csrf": request.session["csrf"], **ctx})

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/webhooks/whatsapp")
    def verify(request: Request):
        """Meta's subscription handshake: echo the challenge only if the verify token matches."""
        q = request.query_params
        if q.get("hub.mode") == "subscribe" and secrets.compare_digest(q.get("hub.verify_token", ""), verify_token):
            return PlainTextResponse(q.get("hub.challenge", ""))
        raise HTTPException(403)

    @app.post("/webhooks/whatsapp")
    async def receive(request: Request, x_hub_signature_256: str = Header(""), conn=Depends(get_db)):
        body = await request.body()
        expected = "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, x_hub_signature_256):
            raise HTTPException(401, "bad signature")
        # the bot calls the Cloud API, which calls this webhook back with statuses: keep the event loop free
        return await run_in_threadpool(bot.handle_webhook, conn, wa, shop, json.loads(body))

    @app.post("/webhooks/shop")
    async def shop_event(request: Request, x_shop_signature: str = Header(""), conn=Depends(get_db)):
        body = await request.body()
        if not hmac.compare_digest(hmac.new(shop_secret.encode(), body, hashlib.sha256).hexdigest(), x_shop_signature):
            raise HTTPException(401, "bad signature")
        ev = json.loads(body)
        return {"result": await run_in_threadpool(bot.shipped, conn, wa, ev["order"]) if ev.get("event") == "order.shipped" else "ignored"}

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
    def inbox(request: Request, view: str = "waiting", user=Depends(current_user), conn=Depends(get_db)):
        where = {"waiting": "c.mode = 'human'", "bot": "c.mode = 'bot'", "closed": "c.mode = 'closed'"}.get(view, "1 = 1")
        rows = conn.execute(f"""select c.*, cu.name, cu.opted_in, cu.last_inbound,
                                (select body from messages m where m.conversation_id = c.id order by m.id desc limit 1) last
                                from conversations c join customers cu on cu.wa_id = c.wa_id where {where} order by c.unread > 0 desc, c.updated_at desc limit 100""").fetchall()
        one = lambda sql: conn.execute(sql).fetchone()[0]
        total = one("select count(*) from conversations")
        stats = {"total": total, "bot": one("select count(*) from conversations where outcome in ('order tracked', 'return created') and assigned_to is null and mode != 'human'"),
                 "human": one("select count(*) from conversations where outcome = 'handed over' or assigned_to is not null"),
                 "returns": one("select count(*) from returns"), "opted": one("select count(*) from customers where opted_in = 1")}
        return page(request, "inbox.html", conn, user=user, rows=rows, view=view, stats=stats, now=time.time())

    @app.get("/c/{cid}", response_class=HTMLResponse)
    def conversation(request: Request, cid: int, user=Depends(current_user), conn=Depends(get_db)):
        c = conn.execute("select c.*, cu.name, cu.opted_in, cu.last_inbound from conversations c join customers cu on cu.wa_id = c.wa_id where c.id = ?", (cid,)).fetchone()
        if not c:
            raise HTTPException(404)
        conn.execute("update conversations set unread = 0 where id = ?", (cid,))
        left = max(0, int((c["last_inbound"] or 0) + bot.WINDOW - time.time()))
        return page(request, "conversation.html", conn, user=user, c=c, left=left,
                    msgs=conn.execute("select * from messages where conversation_id = ? order by id", (cid,)).fetchall(),
                    returns=conn.execute("select * from returns where conversation_id = ?", (cid,)).fetchall(),
                    history=conn.execute("select id, opened_at, outcome from conversations where wa_id = ? and id != ? order by id desc", (c["wa_id"], cid)).fetchall(),
                    error=request.query_params.get("error"))

    @app.post("/c/{cid}/reply", dependencies=[Depends(check_csrf)])
    def reply(cid: int, body: str = Form(""), user=Depends(current_user), conn=Depends(get_db)):
        if body.strip() and not bot.agent_reply(conn, wa, cid, body.strip()[:4096], user["name"]):
            from urllib.parse import quote
            err = conn.execute("select error from messages where conversation_id = ? order by id desc limit 1", (cid,)).fetchone()[0]
            return RedirectResponse(f"/c/{cid}?error=" + quote(f"Not sent: {err}"), 303)
        return RedirectResponse(f"/c/{cid}", 303)

    @app.post("/c/{cid}/template", dependencies=[Depends(check_csrf)])
    def send_template(cid: int, order: str = Form(), user=Depends(current_user), conn=Depends(get_db)):
        c = conn.execute("select c.*, cu.opted_in from conversations c join customers cu on cu.wa_id = c.wa_id where c.id = ?", (cid,)).fetchone()
        o = shop.order(order.strip())
        if not c["opted_in"] or not o or o["phone"] != c["wa_id"]:
            return RedirectResponse(f"/c/{cid}?error=" + "Templates+need+an+opted-in+customer+and+one+of+their+orders.", 303)
        bot.deliver(conn, wa, cid, c["wa_id"], template("order_shipped", "en", [o["number"], o.get("carrier", ""), o.get("tracking_url", "")]), user["name"],
                    f"[template order_shipped] order {o['number']}")
        return RedirectResponse(f"/c/{cid}", 303)

    @app.post("/c/{cid}/mode", dependencies=[Depends(check_csrf)])
    def mode(cid: int, to: str = Form(), user=Depends(current_user), conn=Depends(get_db)):
        if to == "closed":
            bot.close(conn, cid, user["name"])
        elif to == "bot":
            conn.execute("update conversations set mode = 'bot', state = 'menu', assigned_to = null, unread = 0 where id = ?", (cid,))
        elif to == "human":
            conn.execute("update conversations set mode = 'human', assigned_to = ? where id = ?", (user["name"], cid))
        return RedirectResponse(f"/c/{cid}" if to != "closed" else "/", 303)

    return app
