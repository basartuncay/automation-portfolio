"""Pricing team panel: where we stand per product, price history charts, alerts, crawl health, run-now."""
import os, secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import db as store
from .report import positions

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
COLORS = ["#4f46e5", "#ea580c", "#0d9488", "#db2777"]


def chart(series: dict[str, list[tuple[str, float]]], ours: float, w: int = 760, h: int = 260) -> dict:
    """Step-line price history as SVG paths (no JS charting library needed)."""
    points = [pt for s in series.values() for pt in s]
    if not points:
        return {"paths": [], "w": w, "h": h}
    times = sorted({t for t, _ in points})
    lo = min([v for _, v in points] + [ours]) * 0.97
    hi = max([v for _, v in points] + [ours]) * 1.03
    pad = 48
    x = lambda t: pad + (w - pad - 12) * (times.index(t) / max(len(times) - 1, 1))
    y = lambda v: 12 + (h - 40) * (1 - (v - lo) / (hi - lo))
    paths = []
    for i, (name, pts) in enumerate(series.items()):
        d, prev = "", None
        for t, v in pts:
            d += f"M{x(t):.1f},{y(v):.1f}" if prev is None else f"H{x(t):.1f}V{y(v):.1f}"
            prev = v
        if pts:
            d += f"H{w - 12}"
        paths.append({"name": name, "d": d, "color": COLORS[i % len(COLORS)], "last": pts[-1][1] if pts else None})
    ticks = [lo + (hi - lo) * k / 4 for k in range(5)]
    return {"paths": paths, "w": w, "h": h, "ours_y": y(ours), "ticks": [(y(v), v) for v in ticks], "pad": pad,
            "first": times[0], "last": times[-1]}


def create_app(db_path: str | None = None, secret: str | None = None) -> FastAPI:
    app = FastAPI(title="Price Watch", docs_url=None, redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=secret or os.environ["SECRET_KEY"], same_site="strict",
                       https_only=os.environ.get("COOKIE_SECURE", "false").lower() == "true")
    path = db_path or os.environ.get("DB_PATH", "watch.db")
    with store.connect(path) as conn:
        if os.environ.get("COMPETITORS") and os.environ.get("CATALOG"):
            store.load_config(conn, Path(os.environ["COMPETITORS"]), Path(os.environ["CATALOG"]))
        if os.environ.get("DEMO_SEED", "false").lower() == "true":
            store.seed_demo_user(conn)

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
            ctx["open_alerts"] = conn.execute("select count(*) from alerts where at >= datetime('now', '-1 day')").fetchone()[0]
            ctx["pending"] = conn.execute("select count(*) from requests where done = 0").fetchone()[0]
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
    def dashboard(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        pos = positions(conn)
        comps = [r["name"] for r in conn.execute("select name from competitors order by id")]
        alerts = conn.execute("select * from alerts order by id desc limit 6").fetchall()
        health = conn.execute("""select c.name, r.status, r.finished, r.products, r.note from competitors c left join runs r
                                 on r.id = (select max(id) from runs where competitor_id = c.id) order by c.id""").fetchall()
        return page(request, "dashboard.html", conn, user=user, pos=pos, comps=comps, alerts=alerts, health=health,
                    undercut=sum(1 for p in pos if p["gap_pct"] and p["gap_pct"] > 0), active="dash",
                    flash=request.query_params.get("flash"))

    @app.get("/products/{sku}", response_class=HTMLResponse)
    def product(request: Request, sku: str, user=Depends(current_user), conn=Depends(get_db)):
        ours = conn.execute("select * from our_products where sku = ?", (sku,)).fetchone()
        if not ours:
            raise HTTPException(404, "Unknown product")
        series, listings = {}, []
        for c in conn.execute("select * from competitors order by id"):
            p = conn.execute("select * from products where competitor_id = ? and pid = ?", (c["id"], ours["pid"])).fetchone()
            if p:
                series[c["name"]] = [(r["at"], r["price"]) for r in
                                     conn.execute("select at, price from prices where product_id = ? order by id", (p["id"],))]
                listings.append({"competitor": c["name"], **dict(p)})
        alerts = conn.execute("select * from alerts where pid = ? order by id desc", (ours["pid"],)).fetchall()
        return page(request, "product.html", conn, user=user, ours=ours, chart=chart(series, ours["our_price"]),
                    listings=listings, alerts=alerts, active="dash")

    @app.get("/alerts", response_class=HTMLResponse)
    def alerts(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        rows = conn.execute("select * from alerts order by id desc limit 300").fetchall()
        return page(request, "alerts.html", conn, user=user, alerts=rows, active="alerts")

    @app.get("/runs", response_class=HTMLResponse)
    def runs(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        rows = conn.execute("""select r.*, c.name from runs r join competitors c on c.id = r.competitor_id
                               order by r.id desc limit 200""").fetchall()
        return page(request, "runs.html", conn, user=user, runs=rows, active="runs")

    @app.post("/request/{kind}", dependencies=[Depends(check_csrf)])
    def request_job(kind: str, user=Depends(current_user), conn=Depends(get_db)):
        if kind not in ("crawl", "digest"):
            raise HTTPException(404)
        conn.execute("insert into requests (at, kind) values (?, ?)", (store.now(), kind))
        conn.commit()
        msg = "Crawl+started:+results+appear+in+a+few+seconds" if kind == "crawl" else "Weekly+digest+is+being+emailed"
        return RedirectResponse(f"/?flash={msg}", 303)

    return app
