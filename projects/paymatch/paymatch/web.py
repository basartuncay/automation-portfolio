"""Panel: import statements, review the payments that weren't sure enough to book alone, see every match with its
reasons, open and overdue invoices, and the dunning letters sent."""
import json, os, secrets
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import bank, db as store, match
from .db import euro

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else None
templates.env.filters["euro"] = euro


def create_app(db_path: str | None = None, secret: str | None = None) -> FastAPI:
    app = FastAPI(title="Paymatch", docs_url=None, redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=secret or os.environ["SECRET_KEY"], same_site="strict",
                       https_only=os.environ.get("COOKIE_SECURE", "false").lower() == "true")
    path = db_path or os.environ.get("DB_PATH", "paymatch.db")
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
            ctx["n"] = {"review": conn.execute("select count(*) from transactions where status = 'suggested'").fetchone()[0]}
        return templates.TemplateResponse(request, name, {"csrf": request.session["csrf"], **ctx})

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
    def overview(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
        stats = {"auto": one("select count(distinct transaction_id) from matches where auto = 1"),
                 "manual": one("select count(distinct transaction_id) from matches where auto = 0"),
                 "review": one("select count(*) from transactions where status = 'suggested'"),
                 "matched_cents": one("select coalesce(sum(amount_cents), 0) from matches"),
                 "open_cents": one("select coalesce(sum(open_cents), 0) from invoices"),
                 "overdue_cents": one("select coalesce(sum(open_cents), 0) from invoices where open_cents > 0 and due < date('now')"),
                 "unbooked": one("select count(*) from matches where booked = 0"), "errors": one("select count(*) from matches where booking_error is not null and booked = 0")}
        recent = conn.execute("""select m.*, t.name payer, t.purpose, t.booked day, t.amount_cents paid, i.number, i.customer from matches m
                                 join transactions t on t.id = m.transaction_id join invoices i on i.id = m.invoice_id order by m.id desc limit 40""").fetchall()
        return page(request, "overview.html", conn, user=user, active="overview", stats=stats, recent=recent,
                    imports=conn.execute("select * from imports order by id desc limit 5").fetchall(),
                    msg=request.query_params.get("msg"), error=request.query_params.get("error"))

    @app.post("/import", dependencies=[Depends(check_csrf)])
    async def upload(file: UploadFile = File(), user=Depends(current_user), conn=Depends(get_db)):
        data = await file.read(10_000_001)
        if len(data) > 10_000_000:
            return RedirectResponse("/?error=" + quote("Statements up to 10 MB."), 303)
        try:
            r = bank.store(conn, file.filename or "statement", data, user["name"])
        except bank.BadStatement as e:
            return RedirectResponse("/?error=" + quote(str(e)), 303)
        res = match.run(conn)
        return RedirectResponse("/?msg=" + quote(f"{r['format']}: {r['new']} new transactions ({r['duplicates']} already imported). "
                                                 f"{res['auto']} matched automatically, {res['parked']} need a look."), 303)

    @app.get("/review", response_class=HTMLResponse)
    def review(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        rows = conn.execute("select * from transactions where status = 'suggested' order by booked, id").fetchall()
        return page(request, "review.html", conn, user=user, active="review", rows=rows, error=request.query_params.get("error"),
                    invoices=conn.execute("select * from invoices where open_cents > 0 order by customer, issued").fetchall())

    @app.post("/review/{tid}", dependencies=[Depends(check_csrf)])
    async def decide(request: Request, tid: int, user=Depends(current_user), conn=Depends(get_db)):
        form = await request.form()
        t = conn.execute("select * from transactions where id = ? and status = 'suggested'", (tid,)).fetchone()
        if not t:
            return RedirectResponse("/review", 303)
        try:
            if form.get("action") == "ignore":
                conn.execute("update transactions set status = 'ignored', decided_by = ?, decided_at = ? where id = ?", (user["name"], store.now(), tid))
            elif form.get("action") == "manual":
                ids = form.getlist("invoice")
                if not ids:
                    return RedirectResponse("/review?error=" + quote("Pick at least one invoice."), 303)
                match.manual(conn, tid, ids, user["name"])
            else:
                cand = json.loads(t["suggestions"])[int(form.get("pick", 0))]
                match.apply(conn, tid, cand, auto=False, who=user["name"])
        except ValueError as e:
            return RedirectResponse("/review?error=" + quote(str(e)), 303)
        return RedirectResponse("/review", 303)

    @app.post("/dunning", dependencies=[Depends(check_csrf)])
    def run_dunning(user=Depends(current_user), conn=Depends(get_db)):
        from .accounting import dunning
        sent = dunning(conn)
        return RedirectResponse("/invoices?msg=" + quote(f"{len(sent)} reminder(s) queued." if sent else
                                                          "No reminders due (or the bank data is older than 3 days)."), 303)

    @app.get("/invoices", response_class=HTMLResponse)
    def invoices(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        rows = conn.execute("""select i.*, (select max(level) from dunning d where d.invoice_id = i.id) level,
                               (select group_concat(m.kind) from matches m where m.invoice_id = i.id) kinds
                               from invoices i order by i.open_cents = 0, i.due""").fetchall()
        return page(request, "invoices.html", conn, user=user, active="invoices", rows=rows, today=store.now()[:10], msg=request.query_params.get("msg"),
                    letters=conn.execute("select o.*, i.number from outbox o left join invoices i on i.id = o.invoice_id order by o.id desc limit 50").fetchall())

    return app
