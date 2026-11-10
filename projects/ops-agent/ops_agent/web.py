"""Panel: give the agent a task, watch its full trace, approve or reject what it asked for, see what changed, read evals."""
import json, os, secrets
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import agent
from . import db as store
from .engine import make_engine

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["pretty"] = lambda s: json.dumps(json.loads(s) if isinstance(s, str) else s, indent=1, ensure_ascii=False, default=str)
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else None


def create_app(db_path: str | None = None, secret: str | None = None, engine=None, run_inline: bool = False) -> FastAPI:
    app = FastAPI(title="Ops Agent", docs_url=None, redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=secret or os.environ["SECRET_KEY"], same_site="strict",
                       https_only=os.environ.get("COOKIE_SECURE", "false").lower() == "true")
    path = db_path or os.environ.get("DB_PATH", "ops.db")
    engine = engine or make_engine()
    api_token = os.environ.get("API_TOKEN", "")
    report_path = Path(os.environ.get("EVAL_REPORT", "eval-report.json"))
    pool = ThreadPoolExecutor(max_workers=2)
    with store.connect(path) as conn:
        store.seed_shop(conn)
        if os.environ.get("DEMO_SEED", "false").lower() == "true":
            store.seed_users(conn)
        # runs interrupted by a restart while running are resumed; waiting ones keep waiting for their approvals
        stuck = [r["id"] for r in conn.execute("select id from runs where status in ('queued', 'running')")]

    def background(fn, *args):
        def job():
            conn = store.connect(path)
            try:
                fn(conn, engine, *args)
            finally:
                conn.close()
        if run_inline:  # tests: run synchronously so assertions see the result
            job()
        else:
            pool.submit(job)

    for rid in stuck:
        background(agent.advance, rid)

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
            ctx["pending_count"] = conn.execute("select count(*) from approvals where status = 'pending'").fetchone()[0]
        return templates.TemplateResponse(request, name, {"csrf": request.session["csrf"], "engine": engine.name, **ctx})

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
    def runs(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        rows = conn.execute("select * from runs order by id desc limit 100").fetchall()
        return page(request, "runs.html", conn, user=user, runs=rows, cost=agent.cost_usd, active="runs")

    @app.post("/tasks", dependencies=[Depends(check_csrf)])
    def new_task(task: str = Form(), user=Depends(current_user), conn=Depends(get_db)):
        if user["role"] not in ("operator", "manager"):
            raise HTTPException(403, "viewers can't give the agent tasks")
        if not 5 <= len(task.strip()) <= 4000:
            return RedirectResponse("/", 303)
        rid = agent.create_run(conn, task, "panel", engine.name, user["name"])
        background(agent.advance, rid)
        return RedirectResponse(f"/runs/{rid}", 303)

    @app.post("/api/tasks")
    def api_task(body: dict, authorization: str = Header(""), conn=Depends(get_db)):
        """For n8n, a helpdesk webhook or a cron job: {"task": "..."} with 'Authorization: Bearer <API_TOKEN>'."""
        if not api_token or not secrets.compare_digest(authorization.removeprefix("Bearer ").strip(), api_token):
            raise HTTPException(401, "invalid token")
        task = str(body.get("task", "")).strip()
        if not 5 <= len(task) <= 4000:
            raise HTTPException(422, "task must be 5 to 4000 characters")
        rid = agent.create_run(conn, task, "api", engine.name, "API")
        background(agent.advance, rid)
        return {"run_id": rid, "status": "queued"}

    @app.get("/api/runs/{rid}")
    def api_run(rid: int, authorization: str = Header(""), conn=Depends(get_db)):
        if not api_token or not secrets.compare_digest(authorization.removeprefix("Bearer ").strip(), api_token):
            raise HTTPException(401, "invalid token")
        r = conn.execute("select id, status, summary, turns, input_tokens, output_tokens from runs where id = ?", (rid,)).fetchone()
        if not r:
            raise HTTPException(404)
        return dict(r)

    @app.get("/runs/{rid}", response_class=HTMLResponse)
    def run_page(request: Request, rid: int, user=Depends(current_user), conn=Depends(get_db)):
        run = conn.execute("select * from runs where id = ?", (rid,)).fetchone()
        if not run:
            raise HTTPException(404, "No such run")
        steps = conn.execute("select * from steps where run_id = ? order by id", (rid,)).fetchall()
        approvals = {a["id"]: a for a in conn.execute("select * from approvals where run_id = ?", (rid,))}
        return page(request, "run.html", conn, user=user, run=run, steps=steps, approvals=approvals,
                    cost=agent.cost_usd(run), active="runs")

    @app.get("/approvals", response_class=HTMLResponse)
    def approvals(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        rows = conn.execute("select * from approvals order by status = 'pending' desc, id desc limit 100").fetchall()
        return page(request, "approvals.html", conn, user=user, approvals=rows, active="approvals")

    @app.post("/approvals/{aid}", dependencies=[Depends(check_csrf)])
    def decide(aid: int, decision: str = Form(), note: str = Form(""), back: str = Form("/approvals"),
               user=Depends(current_user), conn=Depends(get_db)):
        if user["role"] != "manager":
            raise HTTPException(403, "only managers approve")
        if decision not in ("approve", "reject"):
            raise HTTPException(422)
        resume_id = agent.decide(conn, aid, user, decision == "approve", note)
        if resume_id:
            background(agent.resume, resume_id)
        return RedirectResponse(back if back.startswith("/") and not back.startswith("//") else "/approvals", 303)

    @app.get("/shop", response_class=HTMLResponse)
    def shop(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        q = lambda sql: conn.execute(sql).fetchall()
        return page(request, "shop.html", conn, user=user, active="shop",
                    refunds=q("select * from refunds order by id desc limit 50"),
                    outbox=q("select * from outbox order by id desc limit 50"),
                    restocks=q("select r.*, i.title from restock_requests r join inventory i using (sku) order by r.id desc limit 50"),
                    orders=q("""select o.id, o.status, o.total, c.name, (select coalesce(sum(amount), 0) from refunds r where r.order_id = o.id) refunded
                                from orders o join customers c on c.id = o.customer_id order by o.id"""))

    @app.get("/evals", response_class=HTMLResponse)
    def evals(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        report = json.loads(report_path.read_text()) if report_path.exists() else None
        return page(request, "evals.html", conn, user=user, report=report, active="evals")

    return app
