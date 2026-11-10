"""Dashboard over the last verified build (serving.duckdb, read-only), plus pipeline runs and the data model."""
import hashlib, hmac, json, os, secrets
from datetime import timedelta
from pathlib import Path

import duckdb
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .run import now, paths, runs_db

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else None
templates.env.filters["eur"] = lambda v: "—" if v is None else f"€{float(v):,.0f}"
templates.env.filters["eur2"] = lambda v: "—" if v is None else f"€{float(v):,.2f}"


def hash_pw(pw: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    return salt.hex() + "$" + hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1).hex()


def check_pw(pw: str, stored: str) -> bool:
    return hmac.compare_digest(hash_pw(pw, bytes.fromhex(stored.split("$", 1)[0])), stored)


def chart(rows, w=1000, h=260, pad=46):
    """Daily net revenue as bars, ad spend as a line: server-side SVG, no JS library."""
    if not rows:
        return None
    top = max(max(float(r[1]), float(r[2])) for r in rows) * 1.1 or 1
    bw = (w - pad - 10) / len(rows)
    y = lambda v: 10 + (h - 40) * (1 - float(v) / top)
    bars = [{"x": round(pad + i * bw + 1, 1), "y": round(y(r[1]), 1), "w": round(bw - 2, 1), "h": round(h - 30 - y(r[1]), 1),
             "tip": f"{r[0]}: net €{float(r[1]):,.0f}, spend €{float(r[2]):,.0f}"} for i, r in enumerate(rows)]
    line = " ".join(f"{pad + i * bw + bw / 2:.1f},{y(r[2]):.1f}" for i, r in enumerate(rows))
    ticks = [(round(y(top * k / 4), 1), f"€{top * k / 4:,.0f}") for k in range(5)]
    return {"w": w, "h": h, "pad": pad, "bars": bars, "line": line, "ticks": ticks, "first": str(rows[0][0]), "last": str(rows[-1][0])}


def create_app(secret: str | None = None) -> FastAPI:
    app = FastAPI(title="Revenue Pipeline", docs_url=None, redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=secret or os.environ["SECRET_KEY"], same_site="strict",
                       https_only=os.environ.get("COOKIE_SECURE", "false").lower() == "true")
    db = runs_db()
    if os.environ.get("DEMO_SEED", "false").lower() == "true":
        db.execute("insert into users (email, name, pw_hash) values (?, ?, ?) on conflict(email) do nothing",
                   ("finance@example.com", "Fran Finance", hash_pw("password")))  # local demo account only
        db.commit()
    db.close()

    def get_runs():
        conn = runs_db()
        try:
            yield conn
        finally:
            conn.close()

    def current_user(request: Request, conn=Depends(get_runs)):
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
            ctx["latest"] = conn.execute("select * from runs where status != 'running' order by id desc limit 1").fetchone()
            ctx["verified"] = conn.execute("select * from runs where published = 1 order by id desc limit 1").fetchone()
            ctx["running"] = conn.execute("select 1 from runs where status = 'running'").fetchone() is not None
        return templates.TemplateResponse(request, name, {"csrf": request.session["csrf"], **ctx})

    def serving():
        p = paths()["serving"]
        return duckdb.connect(str(p), read_only=True) if p.exists() else None

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return page(request, "login.html", error=None)

    @app.post("/login", dependencies=[Depends(check_csrf)])
    def login(request: Request, email: str = Form(), password: str = Form(), conn=Depends(get_runs)):
        user = conn.execute("select * from users where email = ?", (email.strip().lower(),)).fetchone()
        if not user or not check_pw(password, user["pw_hash"]):
            return page(request, "login.html", error="Wrong email or password")
        request.session.clear()
        request.session.update(uid=user["id"], csrf=secrets.token_urlsafe(24))
        return RedirectResponse("/", 303)

    @app.post("/logout", dependencies=[Depends(check_csrf)])
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", 303)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, user=Depends(current_user), conn=Depends(get_runs)):
        s = serving()
        if s is None:
            return page(request, "dashboard.html", conn, user=user, active="dash", empty=True)
        try:
            last = s.execute("select max(day) from marts.fct_daily").fetchone()[0]
            cur_from, prev_from = last - timedelta(days=29), last - timedelta(days=59)

            def kpis(a, b):
                r = s.execute("""select sum(net), sum(orders), sum(ad_spend), sum(new_customers), sum(refunds), sum(gross)
                                 from marts.fct_daily where day between ? and ?""", [a, b]).fetchone()
                net, orders, spend, new, refunds, gross = [float(x or 0) for x in r]
                return {"net": net, "orders": orders, "roas": net / spend if spend else None, "cac": spend / new if new else None,
                        "refund_rate": 100 * refunds / gross if gross else None}
            cur, prev = kpis(cur_from, last), kpis(prev_from, cur_from - timedelta(days=1))
            daily = s.execute("select day, net, ad_spend from marts.fct_daily where day >= ? order by day", [prev_from]).fetchall()
            channels = s.execute("""
                with o as (select channel, count(*) orders, sum(net) net, sum(case when is_first_order then 1 else 0 end) new
                           from marts.fct_orders where order_date >= ? group by 1),
                     sp as (select channel, sum(spend) spend from staging.stg_ad_spend where day >= ? group by 1)
                select o.channel, o.orders, o.net, o.new, sp.spend, o.net / nullif(sp.spend, 0), sp.spend / nullif(o.new, 0)
                from o left join sp using (channel) order by o.net desc""", [cur_from, cur_from]).fetchall()
            cohorts = s.execute("select cohort_month, months_since, retention_pct, cohort_size from marts.fct_cohorts order by 1, 2").fetchall()
        finally:
            s.close()
        grid = {}
        for month, since, pct, size in cohorts:
            grid.setdefault((month.strftime("%B %Y"), size), {})[since] = pct
        return page(request, "dashboard.html", conn, user=user, active="dash", empty=False, last=last, cur=cur, prev=prev,
                    chart=chart(daily), channels=channels, cohorts=grid, max_since=max((c[1] for c in cohorts), default=0))

    @app.get("/runs", response_class=HTMLResponse)
    def runs(request: Request, user=Depends(current_user), conn=Depends(get_runs)):
        rows = conn.execute("select * from runs order by id desc limit 50").fetchall()
        return page(request, "runs.html", conn, user=user, runs=rows, active="runs", flash=request.query_params.get("flash"))

    @app.post("/runs", dependencies=[Depends(check_csrf)])
    def run_now(user=Depends(current_user), conn=Depends(get_runs)):
        conn.execute("insert into requests (at) values (?)", (now(),))
        conn.commit()
        return RedirectResponse("/runs?flash=Run+started", 303)

    @app.get("/model", response_class=HTMLResponse)
    def model(request: Request, user=Depends(current_user), conn=Depends(get_runs)):
        s = serving()
        tables = []
        if s is not None:
            try:
                for schema, name, kind in s.execute("""select table_schema, table_name, table_type from information_schema.tables
                                                    where table_schema in ('raw', 'staging', 'intermediate', 'marts')
                                                    order by case table_schema when 'raw' then 0 when 'staging' then 1 when 'intermediate' then 2 else 3 end, 2""").fetchall():
                    n = s.execute(f'select count(*) from "{schema}"."{name}"').fetchone()[0]
                    tables.append({"schema": schema, "name": name, "kind": "view" if kind == "VIEW" else "table", "rows": n})
            finally:
                s.close()
        tests = sorted(p.stem for p in (Path(__file__).parents[1] / "warehouse" / "tests").glob("*.sql"))
        return page(request, "model.html", conn, user=user, tables=tables, tests=tests, active="model")

    return app
