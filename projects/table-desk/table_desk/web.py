"""HTTP surface: the guests' booking page, the phone agent's tool endpoints, the inbound SMS webhook and the staff
panel. Every channel goes through engine.book / engine.cancel, so the rules are the same everywhere."""
import hashlib, json, os, secrets, time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import db as store
from . import engine
from .engine import Invalid, Unavailable
from .fakes import twilio_signature

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
CHANNEL_LABEL = {"online": "Website", "phone": "Phone agent", "staff": "Staff", "waitlist": "Waitlist", "walk_in": "Walk-in"}
templates.env.globals["channel_label"] = CHANNEL_LABEL


class RateLimit:
    """Sliding window per client address. shortcut: in memory, per process; use Redis when running several web replicas."""

    def __init__(self, limit: int, seconds: int):
        self.limit, self.seconds, self.hits = limit, seconds, defaultdict(deque)

    def allow(self, key: str) -> bool:
        q, t = self.hits[key], time.time()
        while q and q[0] < t - self.seconds:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(t)
        return True


def create_app(db_path: str | None = None, secret: str | None = None) -> FastAPI:
    app = FastAPI(title="Table Desk", docs_url=None, redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=secret or os.environ["SECRET_KEY"], same_site="strict",
                       https_only=os.environ.get("COOKIE_SECURE", "false").lower() == "true")
    path = db_path or os.environ.get("DB_PATH", "desk.db")
    agent_token = os.environ["AGENT_TOOL_TOKEN"]
    sms_token = os.environ["SMS_AUTH_TOKEN"]
    public_url = os.environ.get("PUBLIC_URL", "http://127.0.0.1:8098").rstrip("/")
    book_limit = RateLimit(int(os.environ.get("BOOKINGS_PER_HOUR", 5)), 3600)
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
            ctx["s"] = store.settings(conn)
            ctx["n"] = {"inbox": conn.execute("select count(*) from inbox where done = 0").fetchone()[0]
                        + conn.execute("select count(*) from callbacks where done = 0").fetchone()[0],
                        "failed": conn.execute("select count(*) from outbox where status = 'failed'").fetchone()[0],
                        "waitlist": conn.execute("select count(*) from waitlist where status in ('waiting', 'offered') and day >= date('now')").fetchone()[0]}
        return templates.TemplateResponse(request, name, {"csrf": request.session["csrf"], **ctx})

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # ------------------------------------------------------------------ guests
    @app.get("/", response_class=HTMLResponse)
    def book_page(request: Request, conn=Depends(get_db)):
        s = store.settings(conn)
        today = engine.local_now(s).date()
        return page(request, "book.html", s=s, today=today.isoformat(), max_day=(today + timedelta(days=s["booking_horizon_days"])).isoformat())

    @app.get("/api/slots")
    def slots(day: str, party: int, conn=Depends(get_db)):
        try:
            s = store.settings(conn)
            if party > s["online_max_party"]:
                raise Invalid(f"For more than {s['online_max_party']} people please call us, we'll arrange it personally.")
            return {"times": engine.free_times(conn, day, party, "online")}
        except Invalid as e:
            return JSONResponse({"error": str(e)}, 422)

    @app.post("/api/book")
    async def api_book(request: Request, conn=Depends(get_db)):
        if int(request.headers.get("content-length") or 0) > 4096:
            return JSONResponse({"error": "Request too large."}, 413)
        body = await request.json()
        if body.get("website"):  # honeypot: people don't see this field, form-filling bots do
            return JSONResponse({"ok": True, "code": "XXXXXX"})
        client = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0].strip()
        if not book_limit.allow(client):
            return JSONResponse({"error": "Too many bookings from this connection. Please call us."}, 429)
        try:
            b = engine.book(conn, name=str(body.get("name", "")), phone=str(body.get("phone", "")), email=str(body.get("email") or "")[:120] or None,
                            day=str(body.get("day", "")), time=str(body.get("time", "")), party=int(body.get("party") or 0),
                            notes=str(body.get("notes", "")), channel="online", who="guest web")
        except Unavailable as e:
            return JSONResponse({"error": str(e), "alternatives": e.alternatives}, 409)
        except (Invalid, ValueError) as e:
            return JSONResponse({"error": str(e) if isinstance(e, Invalid) else "Please check the form."}, 422)
        return {"ok": True, "code": b["code"], "start": b["start"], "party": b["party"], "manage": f"/b/{b['code']}"}

    def guest_booking(conn, code: str):
        b = conn.execute("select * from bookings where code = ? and anonymized = 0", (code.upper(),)).fetchone()
        if not b:
            raise HTTPException(404, "Booking not found")
        return b

    @app.get("/b/{code}", response_class=HTMLResponse)
    def manage(request: Request, code: str, conn=Depends(get_db)):
        return page(request, "manage.html", s=store.settings(conn), b=guest_booking(conn, code), error=None)

    @app.post("/b/{code}/cancel", response_class=HTMLResponse, dependencies=[Depends(check_csrf)])
    def manage_cancel(request: Request, code: str, phone_end: str = Form(""), conn=Depends(get_db)):
        b = guest_booking(conn, code)
        # the code alone is in links people forward; the last digits of the phone prove it's the guest
        if len(phone_end) < 4 or not b["phone"].endswith(phone_end.strip()):
            return page(request, "manage.html", s=store.settings(conn), b=b, error="Those aren't the last digits of the booking's phone number.")
        try:
            b = engine.cancel(conn, b["id"], "guest web")
        except Invalid as e:
            return page(request, "manage.html", s=store.settings(conn), b=b, error=str(e))
        return page(request, "manage.html", s=store.settings(conn), b=b, error=None)

    # ------------------------------------------------------------- phone agent
    # Custom-function webhooks for a voice-agent platform (Retell, Vapi and similar): {"args": {...}, "call": {"call_id": ...}}.
    # Each answer has a short "result" sentence the agent can say as is.
    def agent_tool(name: str, args: dict, call: dict, conn) -> dict:
        caller = call.get("from_number") or ""
        if name == "check_availability":
            r = engine.check(conn, args["date"], args["time"], int(args["party_size"]), "phone")
            if r["available"]:
                return {**r, "result": f"Yes, we have a table for {args['party_size']} at {args['time']}."}
            alts = r["alternatives"]
            return {**r, "result": f"{args['time']} is full. I could offer {', '.join(alts)}." if alts else "That day is fully booked. I can put you on the waitlist."}
        if name == "create_booking":
            b = engine.book(conn, name=args["name"], phone=args.get("phone") or caller, day=args["date"], time=args["time"],
                            party=int(args["party_size"]), notes=args.get("notes", ""), channel="phone", who=f"phone agent {call.get('call_id', '')}")
            return {"ok": True, "code": b["code"], "result": f"Booked: {b['party']} people on {args['date']} at {args['time']}. "
                                                             f"A confirmation text is on its way."}
        if name == "find_booking":
            phone = engine.normalize_phone(args.get("phone") or caller)
            rows = conn.execute("select code, start, party from bookings where phone = ? and status = 'booked' and start >= ? order by start",
                                (phone, engine.fmt(engine.local_now(store.settings(conn))))).fetchall()
            if not rows:
                return {"bookings": [], "result": "I can't find an upcoming booking for that number."}
            return {"bookings": [dict(r) for r in rows],
                    "result": "I found " + "; ".join(f"{r['party']} people on {r['start'][:10]} at {r['start'][11:]}" for r in rows) + "."}
        if name == "cancel_booking":
            phone = engine.normalize_phone(args.get("phone") or caller)
            b = conn.execute("select * from bookings where phone = ? and status = 'booked' and start like ? || '%' order by start limit 1",
                             (phone, args["date"])).fetchone()
            if not b:
                return {"ok": False, "result": "I can't find a booking for that number on that day."}
            engine.cancel(conn, b["id"], f"phone agent {call.get('call_id', '')}")
            return {"ok": True, "result": f"Done, the booking on {args['date']} at {b['start'][11:]} is cancelled."}
        if name == "join_waitlist":
            wid = engine.join_waitlist(conn, name=args["name"], phone=args.get("phone") or caller, day=args["date"],
                                       earliest=args["earliest"], latest=args["latest"], party=int(args["party_size"]), who="phone agent")
            return {"ok": True, "id": wid, "result": "You're on the waitlist. If a table opens up we'll text you, and you reply YES to take it."}
        if name == "take_message":
            conn.execute("insert into callbacks (name, phone, message, call_id, at) values (?, ?, ?, ?, ?)",
                         (args["name"][:80], args.get("phone") or caller, args["message"][:1000], call.get("call_id"), store.now()))
            return {"ok": True, "result": "I've passed your message on. The team will call you back."}
        raise HTTPException(404, "unknown tool")

    @app.post("/agent/{name}")
    async def agent(name: str, request: Request, authorization: str = Header(""), conn=Depends(get_db)):
        if not secrets.compare_digest(authorization.removeprefix("Bearer ").strip(), agent_token):
            raise HTTPException(401, "bad token")
        body = await request.json()
        args, call = body.get("args") or {}, body.get("call") or {}
        key = hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()
        if call.get("call_id"):  # platforms retry tool calls on timeouts: the same call never books twice
            done = conn.execute("select response from tool_calls where call_id = ? and tool = ? and args_hash = ?", (call["call_id"], name, key)).fetchone()
            if done:
                return json.loads(done[0])
        try:
            out = agent_tool(name, args, call, conn)
        except Unavailable as e:
            alts = e.alternatives
            out = {"ok": False, "alternatives": alts, "result": f"That time just got taken. I could offer {', '.join(alts)}." if alts else "Sorry, that's fully booked now."}
        except Invalid as e:
            out = {"ok": False, "result": str(e)}
        except (KeyError, ValueError, TypeError) as e:
            out = {"ok": False, "result": f"I'm missing some details ({e}). Could you repeat them?"}
        if call.get("call_id") and out.get("ok") is not False:
            conn.execute("insert or ignore into tool_calls values (?, ?, ?, ?, ?)", (call["call_id"], name, key, json.dumps(out), store.now()))
        return out

    # -------------------------------------------------------------- inbound SMS
    @app.post("/webhooks/sms")
    async def sms_webhook(request: Request, conn=Depends(get_db)):
        form = dict(await request.form())
        expected = twilio_signature(sms_token, public_url + "/webhooks/sms", form)
        if not secrets.compare_digest(expected, request.headers.get("x-twilio-signature", "")):
            raise HTTPException(403, "bad signature")
        engine.handle_sms(conn, form.get("From", ""), form.get("Body", ""), form.get("MessageSid", ""))
        return Response('<?xml version="1.0" encoding="UTF-8"?><Response></Response>', media_type="text/xml")

    # -------------------------------------------------------------------- staff
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
        return RedirectResponse("/panel", 303)

    @app.post("/logout", dependencies=[Depends(check_csrf)])
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", 303)

    def back(day: str, msg: str = "", err: str = "") -> RedirectResponse:
        from urllib.parse import urlencode
        return RedirectResponse("/panel?" + urlencode({k: v for k, v in {"day": day, "msg": msg, "err": err}.items() if v}), 303)

    @app.get("/panel", response_class=HTMLResponse)
    def panel(request: Request, day: str = "", user=Depends(current_user), conn=Depends(get_db)):
        s = store.settings(conn)
        try:
            d = date.fromisoformat(day) if day else engine.local_now(s).date()
        except ValueError:
            d = engine.local_now(s).date()
        windows = engine.seating_windows(s, d)
        bookings = conn.execute("""select b.*, group_concat(bt.table_id, '+') tables from bookings b
                                   left join booking_tables bt on bt.booking_id = b.id where b.start like ? || '%'
                                   group by b.id order by b.start, b.id""", (d.isoformat(),)).fetchall()
        tables = conn.execute("select * from tables_ order by area = 'bar', id").fetchall()
        grid = None
        if windows:
            t0 = windows[0][0].replace(minute=0)
            t1 = max(b + timedelta(minutes=60) for _, b in windows)
            t1 = max([t1] + [datetime.strptime(b["end_"], "%Y-%m-%d %H:%M") for b in bookings if b["status"] != "cancelled"])
            span = (t1 - t0).total_seconds()
            pos = lambda t: round(100 * (datetime.strptime(t, "%Y-%m-%d %H:%M") - t0).total_seconds() / span, 3)
            rows = {t["id"]: [] for t in tables}
            for b in bookings:
                if b["status"] == "cancelled" or not b["tables"]:
                    continue
                for tid in b["tables"].split("+"):
                    rows[tid].append({"b": b, "left": pos(b["start"]), "width": round(pos(b["end_"]) - pos(b["start"]), 3)})
            hours = []
            h = t0
            while h <= t1:
                hours.append({"label": f"{h:%H}:00", "left": pos(engine.fmt(h))})
                h += timedelta(hours=1)
            closed = []
            prev = t0
            for a, b in windows:  # shade the hours nobody can be seated
                if a > prev:
                    closed.append({"left": pos(engine.fmt(prev)), "width": round(pos(engine.fmt(a)) - pos(engine.fmt(prev)), 3)})
                prev = b + timedelta(minutes=60)
            now_ = engine.local_now(s)
            grid = {"rows": rows, "hours": hours, "closed": closed,
                    "now": pos(engine.fmt(now_)) if now_.date() == d and t0 <= now_ <= t1 else None}
        live = [b for b in bookings if b["status"] in ("booked", "seated", "done")]
        stats = {"bookings": len(live), "covers": sum(b["party"] for b in live),
                 "phone": sum(b["channel"] == "phone" for b in live), "online": sum(b["channel"] == "online" for b in live),
                 "no_show": sum(b["status"] == "no_show" for b in bookings), "cancelled": sum(b["status"] == "cancelled" for b in bookings)}
        return page(request, "panel.html", conn, user=user, active="day", day=d, prev=(d - timedelta(days=1)).isoformat(),
                    next=(d + timedelta(days=1)).isoformat(), windows=windows, bookings=bookings, tables=tables, grid=grid, stats=stats,
                    msg=request.query_params.get("msg"), err=request.query_params.get("err"),
                    times=[f"{h:02d}:{m:02d}" for h in range(10, 24) for m in range(0, 60, s["slot_minutes"])])

    @app.post("/panel/bookings", dependencies=[Depends(check_csrf)])
    def staff_book(day: str = Form(), time: str = Form(), party: int = Form(), name: str = Form(), phone: str = Form(""),
                   notes: str = Form(""), tables: str = Form(""), walk_in: str = Form(""), user=Depends(current_user), conn=Depends(get_db)):
        try:
            b = engine.book(conn, name=name, phone=phone or "+490000000000", day=day, time=time, party=party, notes=notes,
                            channel="walk_in" if walk_in else "staff", who=user["name"], tables=[t for t in tables.split("+") if t] or None)
            if walk_in:
                engine.set_status(conn, b["id"], "seated", user["name"])
        except Unavailable as e:
            return back(day, err=str(e) + (f" Free nearby: {', '.join(e.alternatives)}." if e.alternatives else ""))
        except Invalid as e:
            return back(day, err=str(e))
        return back(day, msg=f"Booked {b['name']}, {b['party']} people at {b['start'][11:]} (code {b['code']}).")

    @app.post("/panel/bookings/{bid}", dependencies=[Depends(check_csrf)])
    def staff_update(bid: int, action: str = Form(), user=Depends(current_user), conn=Depends(get_db)):
        b = conn.execute("select * from bookings where id = ?", (bid,)).fetchone()
        if not b:
            raise HTTPException(404)
        try:
            if action == "cancel":
                engine.cancel(conn, bid, user["name"])
            else:
                engine.set_status(conn, bid, action, user["name"])
        except Invalid as e:
            return back(b["start"][:10], err=str(e))
        return back(b["start"][:10], msg=f"{b['name']}: {action.replace('_', ' ')}.")

    @app.get("/panel/bookings/{bid}", response_class=HTMLResponse)
    def booking_detail(request: Request, bid: int, user=Depends(current_user), conn=Depends(get_db)):
        b = conn.execute("select b.*, group_concat(bt.table_id, '+') tables from bookings b left join booking_tables bt on bt.booking_id = b.id where b.id = ? group by b.id", (bid,)).fetchone()
        if not b:
            raise HTTPException(404)
        return page(request, "booking.html", conn, user=user, active="day", b=b,
                    history=conn.execute("select * from audit where booking_id = ? order by id", (bid,)).fetchall(),
                    msgs=conn.execute("select * from outbox where booking_id = ? order by id", (bid,)).fetchall())

    @app.get("/panel/waitlist", response_class=HTMLResponse)
    def waitlist(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        rows = conn.execute("select * from waitlist where day >= date('now', '-1 day') order by day, id").fetchall()
        return page(request, "waitlist.html", conn, user=user, active="waitlist", rows=rows, err=request.query_params.get("err"))

    @app.post("/panel/waitlist", dependencies=[Depends(check_csrf)])
    def waitlist_add(day: str = Form(), earliest: str = Form(), latest: str = Form(), party: int = Form(), name: str = Form(), phone: str = Form(),
                     user=Depends(current_user), conn=Depends(get_db)):
        try:
            engine.join_waitlist(conn, name=name, phone=phone, day=day, earliest=earliest, latest=latest, party=party, who=user["name"])
        except Invalid as e:
            from urllib.parse import quote
            return RedirectResponse("/panel/waitlist?err=" + quote(str(e)), 303)
        engine.offer_waitlist(conn, day)
        return RedirectResponse("/panel/waitlist", 303)

    @app.post("/panel/waitlist/{wid}/remove", dependencies=[Depends(check_csrf)])
    def waitlist_remove(wid: int, user=Depends(current_user), conn=Depends(get_db)):
        w = conn.execute("select * from waitlist where id = ?", (wid,)).fetchone()
        conn.execute("update waitlist set status = 'cancelled' where id = ? and status in ('waiting', 'offered')", (wid,))
        if w and w["status"] == "offered":
            engine.offer_waitlist(conn, w["day"])
        return RedirectResponse("/panel/waitlist", 303)

    @app.get("/panel/messages", response_class=HTMLResponse)
    def messages(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        return page(request, "messages.html", conn, user=user, active="messages",
                    inbox=conn.execute("select * from inbox order by done, id desc limit 50").fetchall(),
                    callbacks=conn.execute("select * from callbacks order by done, id desc limit 50").fetchall(),
                    outbox=conn.execute("select * from outbox order by id desc limit 80").fetchall())

    @app.post("/panel/messages/{kind}/{mid}/done", dependencies=[Depends(check_csrf)])
    def message_done(kind: str, mid: int, user=Depends(current_user), conn=Depends(get_db)):
        if kind not in ("inbox", "callbacks"):
            raise HTTPException(404)
        conn.execute(f"update {kind} set done = 1 where id = ?", (mid,))
        return RedirectResponse("/panel/messages", 303)

    @app.post("/panel/messages/outbox/{mid}/retry", dependencies=[Depends(check_csrf)])
    def message_retry(mid: int, user=Depends(current_user), conn=Depends(get_db)):
        conn.execute("update outbox set status = 'queued', attempts = 0, due = ? where id = ? and status = 'failed'", (time.time(), mid))
        return RedirectResponse("/panel/messages", 303)

    @app.get("/panel/setup", response_class=HTMLResponse)
    def setup(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        return page(request, "setup.html", conn, user=user, active="setup", tables=conn.execute("select * from tables_ order by id").fetchall(),
                    combos=conn.execute("select * from combos order by id").fetchall(),
                    days=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])

    return app
