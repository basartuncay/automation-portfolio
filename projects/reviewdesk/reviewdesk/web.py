"""The manager's panel: reviews waiting for approval (serious ones first), replies scheduled to post, what was
posted, and ratings per location."""
import json, os, secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from . import db as store
from . import desk

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else []
TABS = [("awaiting_approval", "Needs approval"), ("scheduled", "Scheduled"), ("posted", "Posted"), ("replied_elsewhere", "Answered elsewhere"), ("skipped", "Skipped")]


def create_app(db_path: str | None = None, secret: str | None = None, google=None) -> FastAPI:
    app = FastAPI(title="Reviewdesk", docs_url=None, redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=secret or os.environ["SECRET_KEY"], same_site="strict",
                       https_only=os.environ.get("COOKIE_SECURE", "false").lower() == "true")
    path = db_path or os.environ.get("DB_PATH", "reviewdesk.db")
    google = google or desk.Google()
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
            ctx["counts"] = {r[0]: r[1] for r in conn.execute("select status, count(*) from reviews group by status")}
        return templates.TemplateResponse(request, name, {"csrf": request.session["csrf"], "tabs": TABS, **ctx})

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

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
    def inbox(request: Request, status: str = "awaiting_approval", user=Depends(current_user), conn=Depends(get_db)):
        rows = conn.execute("""select r.*, l.name loc from reviews r join locations l on l.id = r.location_id where r.status = ?
                               order by r.urgent is null, r.stars, r.created desc""", (status,)).fetchall()
        return page(request, "inbox.html", conn, user=user, rows=rows, status=status, msg=request.query_params.get("msg"))

    @app.post("/sync", dependencies=[Depends(check_csrf)])
    async def sync_now(user=Depends(current_user), conn=Depends(get_db)):
        r = await run_in_threadpool(desk.sync, conn, google)
        return RedirectResponse(f"/?msg={r['new']}+new,+{r['approval']}+need+approval,+{r['scheduled']}+scheduled", 303)

    @app.post("/reviews/{rid}", dependencies=[Depends(check_csrf)])
    async def decide(rid: str, action: str = Form(), text: str = Form(""), user=Depends(current_user), conn=Depends(get_db)):
        r = conn.execute("select * from reviews where id = ?", (rid,)).fetchone()
        if not r or r["status"] not in ("awaiting_approval", "scheduled"):
            return RedirectResponse("/", 303)
        if action == "skip":
            conn.execute("update reviews set status = 'skipped', decided_by = ? where id = ?", (user["name"], rid))
            store.audit(conn, rid, user["name"], "skipped: no reply")
        else:
            body = text.strip() or r["draft"]
            if not await run_in_threadpool(desk.post, conn, google, rid, body[:4000], user["name"]):
                return RedirectResponse(f"/?status={r['status']}&msg=Google+refused+the+reply;+try+again", 303)
        return RedirectResponse(f"/?status={r['status']}&msg=" + ("Skipped." if action == "skip" else "Reply+posted+on+Google."), 303)

    @app.get("/stats", response_class=HTMLResponse)
    def stats(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        return page(request, "stats.html", conn, user=user, status="stats", rows=desk.stats(conn))

    return app
