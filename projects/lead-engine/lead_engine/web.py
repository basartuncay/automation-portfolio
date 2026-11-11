"""Panel and public endpoints: the funnel, leads with their score explained, CSV imports with a merge report, the
sequence, scoring rules, the suppression list, the one-click unsubscribe page and a signed web-form webhook."""
import hashlib, hmac, json, os, secrets
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import db as store
from .intake import BadFile, import_csv, normalize, upsert
from .outreach import check_token, enroll, suppress

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else None
FUNNEL = ["new", "qualified", "in_sequence", "finished", "interested", "not_interested", "unsubscribed", "bounced", "disqualified"]


def create_app(db_path: str | None = None, secret: str | None = None) -> FastAPI:
    app = FastAPI(title="Lead Engine", docs_url=None, redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=secret or os.environ["SECRET_KEY"], same_site="strict",
                       https_only=os.environ.get("COOKIE_SECURE", "false").lower() == "true")
    path = db_path or os.environ.get("DB_PATH", "leads.db")
    form_secret = os.environ["FORM_WEBHOOK_SECRET"]
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
            ctx["n"] = {"interested": conn.execute("select count(*) from leads where status = 'interested'").fetchone()[0]}
        return templates.TemplateResponse(request, name, {"csrf": request.session["csrf"], **ctx})

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # ---- public ----
    @app.get("/u/{token}", response_class=HTMLResponse)
    def unsubscribe_page(request: Request, token: str, conn=Depends(get_db)):
        lid = check_token(token)
        if not lid:
            raise HTTPException(404)
        done = conn.execute("select status from leads where id = ?", (lid,)).fetchone()[0] == "unsubscribed"
        return page(request, "unsubscribe.html", token=token, done=done)

    @app.post("/u/{token}", response_class=HTMLResponse)
    def unsubscribe(request: Request, token: str, conn=Depends(get_db)):
        """One click from the page or from the mail client (RFC 8058 POST). The signed token is the only credential."""
        lid = check_token(token)
        if not lid:
            raise HTTPException(404)
        lead = conn.execute("select * from leads where id = ?", (lid,)).fetchone()
        suppress(conn, lead["email"], "unsubscribed via link", "recipient")
        conn.execute("update enrollments set status = 'stopped', stop_reason = 'unsubscribed' where lead_id = ? and status = 'active'", (lid,))
        if lead["status"] != "unsubscribed":
            store.set_status(conn, lid, "unsubscribed", "clicked the unsubscribe link")
        return page(request, "unsubscribe.html", token=token, done=True)

    @app.post("/webhooks/form")
    async def form_webhook(request: Request, x_signature: str = Header("")):
        """Inbound leads from a website form tool. Signed: X-Signature: sha256=<hex HMAC of the raw body>."""
        body = await request.body()
        expected = "sha256=" + hmac.new(form_secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, x_signature):
            raise HTTPException(401, "bad signature")
        raw = json.loads(body)
        conn = store.connect(path)
        try:
            lead = normalize({"email": raw.get("email"), "first_name": raw.get("first_name"), "last_name": raw.get("last_name"),
                              "company": raw.get("company"), "title": raw.get("title"), "website": raw.get("website"), "country": raw.get("country")})
            outcome, lid, _ = upsert(conn, lead, "website form", raw)
        finally:
            conn.close()
        return {"outcome": outcome, "id": lid}

    # ---- staff ----
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
    def dashboard(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        counts = {r[0]: r[1] for r in conn.execute("select status, count(*) from leads group by status")}
        total = sum(counts.values())
        contacted = conn.execute("select count(distinct lead_id) from emails where direction = 'out'").fetchone()[0]
        replied = conn.execute("select count(distinct lead_id) from emails where direction = 'in' and classification != 'bounce' and classification != 'auto_reply'").fetchone()[0]
        steps = [("Imported", total), ("Qualified", total - counts.get("new", 0) - counts.get("disqualified", 0)), ("Emailed", contacted),
                 ("Replied", replied), ("Interested", counts.get("interested", 0))]
        recent = conn.execute("""select e.*, l.email, l.first_name, l.last_name, l.company from events e join leads l on l.id = e.lead_id
                                 where e.kind in ('status', 'email_sent', 'auto_reply', 'crm') order by e.id desc limit 14""").fetchall()
        sent_today = (conn.execute("select n from sent_counts order by day desc limit 1").fetchone() or [0])[0]
        return page(request, "dashboard.html", conn, user=user, active="dash", steps=steps, counts=counts, funnel=FUNNEL, recent=recent,
                    s=store.settings(conn), sent_today=sent_today, top=max([s[1] for s in steps] + [1]))

    @app.get("/leads", response_class=HTMLResponse)
    def leads(request: Request, status: str = "", q: str = "", user=Depends(current_user), conn=Depends(get_db)):
        sql, args = "select * from leads where 1 = 1", []
        if status:
            sql, args = sql + " and status = ?", args + [status]
        if q:
            sql, args = sql + " and (email like ? or company like ? or last_name like ?)", args + [f"%{q}%"] * 3
        rows = conn.execute(sql + " order by coalesce(score, -999) desc, id limit 300", args).fetchall()
        counts = {r[0]: r[1] for r in conn.execute("select status, count(*) from leads group by status")}
        return page(request, "leads.html", conn, user=user, active="leads", rows=rows, status=status, q=q, counts=counts, funnel=FUNNEL)

    @app.get("/leads/{lid}", response_class=HTMLResponse)
    def lead(request: Request, lid: int, user=Depends(current_user), conn=Depends(get_db)):
        l = conn.execute("select * from leads where id = ?", (lid,)).fetchone()
        if not l:
            raise HTTPException(404)
        return page(request, "lead.html", conn, user=user, active="leads", l=l,
                    events=conn.execute("select * from events where lead_id = ? order by id", (lid,)).fetchall(),
                    emails=conn.execute("select * from emails where lead_id = ? order by id", (lid,)).fetchall(),
                    sources=conn.execute("select * from lead_sources where lead_id = ? order by id", (lid,)).fetchall(),
                    enrollment=conn.execute("select * from enrollments where lead_id = ? order by id desc limit 1", (lid,)).fetchone())

    @app.post("/leads/{lid}", dependencies=[Depends(check_csrf)])
    def lead_action(lid: int, action: str = Form(), user=Depends(current_user), conn=Depends(get_db)):
        l = conn.execute("select * from leads where id = ?", (lid,)).fetchone()
        if not l:
            raise HTTPException(404)
        if action == "enroll" and l["status"] in ("qualified", "disqualified", "finished"):
            enroll(conn, lid, by=user["name"])
        elif action == "stop":
            conn.execute("update enrollments set status = 'stopped', stop_reason = ? where lead_id = ? and status = 'active'", (f"stopped by {user['name']}", lid))
            store.set_status(conn, lid, "finished", f"stopped by {user['name']}")
        elif action == "suppress":
            suppress(conn, l["email"], f"suppressed by {user['name']}", user["name"])
            conn.execute("update enrollments set status = 'stopped', stop_reason = 'suppressed' where lead_id = ? and status = 'active'", (lid,))
            store.set_status(conn, lid, "unsubscribed", f"suppressed by {user['name']}")
        elif action == "interested":
            conn.execute("update enrollments set status = 'stopped', stop_reason = 'marked interested' where lead_id = ? and status = 'active'", (lid,))
            store.set_status(conn, lid, "interested", f"marked by {user['name']}")
        return RedirectResponse(f"/leads/{lid}", 303)

    @app.get("/imports", response_class=HTMLResponse)
    def imports(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        return page(request, "imports.html", conn, user=user, active="imports", rows=conn.execute("select * from imports order by id desc").fetchall(),
                    error=request.query_params.get("error"))

    @app.post("/imports", dependencies=[Depends(check_csrf)])
    async def upload(file: UploadFile = File(), user=Depends(current_user), conn=Depends(get_db)):
        data = await file.read(5_000_001)
        if len(data) > 5_000_000:
            return RedirectResponse("/imports?error=" + quote("Files up to 5 MB, please split bigger exports."), 303)
        try:
            r = import_csv(conn, file.filename or "upload.csv", data, user["name"])
        except BadFile as e:
            return RedirectResponse("/imports?error=" + quote(str(e)), 303)
        return RedirectResponse(f"/imports/{r['id']}", 303)

    @app.get("/imports/{iid}", response_class=HTMLResponse)
    def import_detail(request: Request, iid: int, user=Depends(current_user), conn=Depends(get_db)):
        i = conn.execute("select * from imports where id = ?", (iid,)).fetchone()
        if not i:
            raise HTTPException(404)
        return page(request, "import.html", conn, user=user, active="imports", i=i, report=json.loads(i["report"] or "{}"))

    @app.get("/setup", response_class=HTMLResponse)
    def setup(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        seq = conn.execute("select * from sequences where active = 1 order by id limit 1").fetchone()
        return page(request, "setup.html", conn, user=user, active="setup", s=store.settings(conn), seq=seq, steps=json.loads(seq["steps"]),
                    rules_json=json.dumps(store.settings(conn)["rules"], indent=2, ensure_ascii=False),
                    suppressions=conn.execute("select * from suppressions order by at desc limit 200").fetchall(),
                    msg=request.query_params.get("msg"), error=request.query_params.get("error"))

    @app.post("/setup/rules", dependencies=[Depends(check_csrf)])
    def save_rules(rules: str = Form(), qualify_at: int = Form(), user=Depends(current_user), conn=Depends(get_db)):
        try:
            parsed = json.loads(rules)
            ok = isinstance(parsed, list) and all(isinstance(r, dict) and r.get("when") in ("industry", "tech", "employees", "country", "title", "email")
                                                  and isinstance(r.get("points"), int) and r.get("label") and len({"in", "between", "contains", "is"} & r.keys()) == 1
                                                  for r in parsed)
        except json.JSONDecodeError as e:
            return RedirectResponse("/setup?error=" + quote(f"Rules aren't valid JSON: {e}"), 303)
        if not ok:
            return RedirectResponse("/setup?error=" + quote("Each rule needs when, label, integer points and exactly one of in/between/contains/is."), 303)
        conn.execute("update settings set value = ? where key = 'rules'", (json.dumps(parsed),))
        conn.execute("update settings set value = ? where key = 'qualify_at'", (str(qualify_at),))
        return RedirectResponse("/setup?msg=" + quote("Rules saved. They apply to leads scored from now on."), 303)

    @app.post("/setup/sequence", dependencies=[Depends(check_csrf)])
    async def save_sequence(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        form = await request.form()
        seq = conn.execute("select * from sequences where active = 1 order by id limit 1").fetchone()
        steps = json.loads(seq["steps"])
        for i, st in enumerate(steps):
            st["subject"] = str(form.get(f"subject{i}", st["subject"]))[:200] if i == 0 else ""
            st["body"] = str(form.get(f"body{i}", st["body"]))[:5000]
            st["delay_days"] = max(0, int(form.get(f"delay{i}", st["delay_days"]) or 0)) if i else 0
            try:
                st["body"].format_map(defaultdict(str))
            except (ValueError, IndexError) as e:
                return RedirectResponse("/setup?error=" + quote(f"Step {i + 1}: {e}. Use {{first_name}}, {{company}} or {{tech}}."), 303)
        conn.execute("update sequences set steps = ? where id = ?", (json.dumps(steps), seq["id"]))
        return RedirectResponse("/setup?msg=" + quote("Sequence saved."), 303)

    @app.post("/setup/suppressions", dependencies=[Depends(check_csrf)])
    def add_suppression(value: str = Form(), user=Depends(current_user), conn=Depends(get_db)):
        v = value.strip().lower().removeprefix("@")
        if v:
            suppress(conn, v, f"added by {user['name']}", user["name"])
        return RedirectResponse("/setup?msg=" + quote(f"{v} won't be contacted."), 303)

    return app
