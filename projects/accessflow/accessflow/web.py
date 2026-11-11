"""HR webhooks, the IT panel (people, access, actions, drift, audit export), and the manager's approval page."""
import csv, hashlib, hmac, io, json, os, secrets
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from . import db as store
from . import engine, hris
from .db import PRIVILEGED, RULES

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals["phase"] = engine.phase


def approval_token(grant_id: int, manager_email: str) -> str:
    """The approval link is signed for one grant and one manager: IT staff can't approve their own requests by guessing it."""
    return f"{grant_id}.{hmac.new(os.environ['SECRET_KEY'].encode(), f'{grant_id}:{manager_email}'.encode(), hashlib.sha256).hexdigest()[:32]}"


def create_app(db_path: str | None = None, secret: str | None = None, clients=None) -> FastAPI:
    app = FastAPI(title="Accessflow", docs_url=None, redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=secret or os.environ["SECRET_KEY"], same_site="strict",
                       https_only=os.environ.get("COOKIE_SECURE", "false").lower() == "true")
    path = db_path or os.environ.get("DB_PATH", "accessflow.db")
    hook_secret = os.environ["HRIS_WEBHOOK_SECRET"]
    public = os.environ.get("PUBLIC_URL", "http://127.0.0.1:8110").rstrip("/")
    if clients is None:
        from .targets import GitHub, Google, Slack
        clients = {"google": Google(), "slack": Slack(), "github": GitHub()}
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
            ctx["n"] = {"approvals": conn.execute("select count(*) from grants where state = 'awaiting_approval' and approved_by is null").fetchone()[0],
                        "drift": conn.execute("select count(*) from drift where status = 'open'").fetchone()[0],
                        "failed": conn.execute("select count(*) from actions where status = 'failed'").fetchone()[0]}
        return templates.TemplateResponse(request, name, {"csrf": request.session["csrf"], **ctx})

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.post("/webhooks/hris")
    async def hris_hook(request: Request, x_hris_signature: str = Header(""), conn=Depends(get_db)):
        body = await request.body()
        if not hmac.compare_digest(hmac.new(hook_secret.encode(), body, hashlib.sha256).hexdigest(), x_hris_signature):
            raise HTTPException(401, "bad signature")
        ev = json.loads(body)
        if conn.execute("insert or ignore into webhook_ids values (?, ?)", (ev["id"], store.now())).rowcount == 0:
            return {"duplicate": True}
        return {"planned": await run_in_threadpool(hris.apply, conn, ev["employee"], f"HR system ({ev['event']})")}

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
    def people(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        emps = conn.execute("select * from employees order by end_date is null, name").fetchall()
        access = {}
        for g in conn.execute("select employee_id, system, resource from grants where state = 'granted'"):
            access.setdefault(g["employee_id"], set()).add((g["system"], g["resource"]))
        pending = {r[0]: r[1] for r in conn.execute("select employee_id, count(*) from actions where status = 'pending' group by 1")}
        return page(request, "people.html", conn, user=user, active="people", emps=emps, access=access, pending=pending)

    @app.get("/people/{eid}", response_class=HTMLResponse)
    def person(request: Request, eid: str, user=Depends(current_user), conn=Depends(get_db)):
        e = conn.execute("select * from employees where id = ?", (eid,)).fetchone()
        if not e:
            raise HTTPException(404)
        return page(request, "person.html", conn, user=user, active="people", e=e, privileged=sorted(PRIVILEGED),
                    manager=conn.execute("select * from employees where id = ?", (e["manager_id"],)).fetchone() if e["manager_id"] else None,
                    grants=conn.execute("select * from grants where employee_id = ? order by system, resource", (eid,)).fetchall(),
                    actions=conn.execute("select * from actions where employee_id = ? order by id desc", (eid,)).fetchall(),
                    log=conn.execute("select * from audit where employee_id = ? order by id desc", (eid,)).fetchall(), msg=request.query_params.get("msg"))

    @app.post("/people/{eid}/request", dependencies=[Depends(check_csrf)])
    def request_access(eid: str, access: str = Form(), user=Depends(current_user), conn=Depends(get_db)):
        system, resource = access.split(" ", 1)
        engine.request_privileged(conn, eid, system, resource, user["name"])
        e = conn.execute("select * from employees where id = ?", (eid,)).fetchone()
        mgr = conn.execute("select * from employees where id = ?", (e["manager_id"],)).fetchone()
        gid = conn.execute("select id from grants where employee_id = ? and system = ? and resource = ?", (eid, system, resource)).fetchone()[0]
        link = f"{public}/approve/{approval_token(gid, mgr['email'])}"
        sent = clients["slack"].dm(mgr["email"], f"{user['name']} asks for *{system} {resource}* for {e['name']}. Approve or deny: {link}")
        store.audit(conn, eid, user["name"], f"approval request sent to {mgr['name']} on Slack" if sent else f"couldn't reach {mgr['name']} on Slack")
        return RedirectResponse(f"/people/{eid}?msg=" + quote(f"Approval request sent to {mgr['name']}."), 303)

    def grant_for(token: str, conn):
        gid = token.split(".", 1)[0]
        g = conn.execute("""select g.*, e.name, e.manager_id, m.email mgr_email, m.name mgr_name from grants g join employees e on e.id = g.employee_id
                            join employees m on m.id = e.manager_id where g.id = ?""", (gid if gid.isdigit() else -1,)).fetchone()
        if not g or not hmac.compare_digest(approval_token(g["id"], g["mgr_email"]), token):
            raise HTTPException(404)
        return g

    @app.get("/approve/{token}", response_class=HTMLResponse)
    def approval_page(request: Request, token: str, conn=Depends(get_db)):
        return page(request, "approve.html", g=grant_for(token, conn), token=token, done=None)

    @app.post("/approve/{token}", response_class=HTMLResponse)
    def approval_decide(request: Request, token: str, decision: str = Form(), conn=Depends(get_db)):
        g = grant_for(token, conn)
        if g["state"] != "awaiting_approval" or g["approved_by"]:
            return page(request, "approve.html", g=g, token=token, done="This request was already decided.")
        result = engine.decide(conn, g["id"], g["mgr_email"], decision == "approve")
        return page(request, "approve.html", g=g, token=token, done=f"{result.capitalize()}. Thank you.")

    @app.get("/actions", response_class=HTMLResponse)
    def actions(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        rows = conn.execute("select a.*, e.name from actions a join employees e on e.id = a.employee_id order by a.id desc limit 200").fetchall()
        return page(request, "actions.html", conn, user=user, active="actions", rows=rows)

    @app.post("/actions/{aid}/retry", dependencies=[Depends(check_csrf)])
    def retry(aid: int, user=Depends(current_user), conn=Depends(get_db)):
        conn.execute("update actions set status = 'pending', attempts = 0, next_at = 0 where id = ? and status = 'failed'", (aid,))
        return RedirectResponse("/actions", 303)

    @app.get("/drift", response_class=HTMLResponse)
    def drift(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        return page(request, "drift.html", conn, user=user, active="drift", rows=conn.execute("select * from drift order by status = 'open' desc, id desc limit 100").fetchall(),
                    error=request.query_params.get("error"))

    @app.post("/drift/scan", dependencies=[Depends(check_csrf)])
    async def drift_scan(user=Depends(current_user), conn=Depends(get_db)):
        await run_in_threadpool(engine.scan, conn, clients)
        return RedirectResponse("/drift", 303)

    @app.post("/drift/{did}", dependencies=[Depends(check_csrf)])
    async def drift_fix(did: int, action: str = Form(), user=Depends(current_user), conn=Depends(get_db)):
        d = conn.execute("select * from drift where id = ? and status = 'open'", (did,)).fetchone()
        if not d:
            return RedirectResponse("/drift", 303)
        if action == "remove":
            fn = {"google": clients["google"].suspend, "slack": clients["slack"].deactivate, "github": clients["github"].remove}[d["system"]]
            if "admins team" in d["detail"]:
                fn = lambda login: clients["github"].team("admins", login, False)
            result = await run_in_threadpool(fn, d["account"])
            store.audit(conn, None, user["name"], f"removed drift: {d['system']} {d['account']} ({result})")
        else:
            store.audit(conn, None, user["name"], f"accepted: {d['system']} {d['account']} stays (reason: known exception)")
        conn.execute("update drift set status = 'resolved', resolved_by = ?, resolved_at = ? where id = ?", (f"{user['name']}: {action}", store.now(), did))
        return RedirectResponse("/drift", 303)

    @app.get("/rules", response_class=HTMLResponse)
    def rules(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        return page(request, "rules.html", conn, user=user, active="rules", rules=RULES, privileged=sorted(PRIVILEGED))

    @app.get("/audit.csv")
    def audit_csv(user=Depends(current_user), conn=Depends(get_db)):
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["time (UTC)", "employee", "by", "what"])
        for r in conn.execute("select a.at, coalesce(e.name, ''), a.who, a.what from audit a left join employees e on e.id = a.employee_id order by a.id"):
            w.writerow(list(r))
        return Response(out.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=access-audit.csv"})

    return app
