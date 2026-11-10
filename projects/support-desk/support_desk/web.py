"""The support team's panel: queue, ticket review with the AI draft and its sources, approve & send, knowledge gaps."""
import json, os, secrets, smtplib
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from doc_qa.core import load_folder

from . import db as store
from .engine import make_engine

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
STATUSES = {"drafted": "Draft ready", "needs_human": "Needs a person", "routed": "Routed", "sent": "Sent", "closed": "Closed"}
templates.env.globals["STATUSES"] = STATUSES
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else None


def smtp_send(msg: EmailMessage) -> None:
    e = os.environ
    with smtplib.SMTP(e.get("SMTP_HOST", "localhost"), int(e.get("SMTP_PORT", 587)), timeout=20) as s:
        if e.get("SMTP_STARTTLS", "true").lower() == "true":
            s.starttls()
        if e.get("SMTP_USER"):
            s.login(e["SMTP_USER"], e["SMTP_PASSWORD"])
        s.send_message(msg)


def can_send(user, t) -> str | None:
    """Why this user may not send this reply, or None if they may."""
    if user["role"] not in ("agent", "lead"):
        return "viewers can't send replies"
    if t["status"] not in ("drafted", "needs_human"):
        return "this ticket is already handled"
    if user["role"] != "lead" and (t["urgency"] == "high" or t["injection"]):
        return "high-urgency and flagged emails need a lead"
    return None


def create_app(db_path: str | None = None, send=smtp_send, secret: str | None = None) -> FastAPI:
    app = FastAPI(title="Support Desk", docs_url=None, redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=secret or os.environ["SECRET_KEY"], same_site="strict",
                       https_only=os.environ.get("COOKIE_SECURE", "false").lower() == "true")
    path = db_path or os.environ.get("DB_PATH", "support.db")
    with store.connect(path) as conn:
        if os.environ.get("DEMO_SEED", "false").lower() == "true":
            store.seed_demo_users(conn)
    kb_dir = Path(os.environ.get("KB_DIR", "kb"))
    engine_name = make_engine().name
    support_from = os.environ.get("SUPPORT_FROM", "support@example.com")

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

    def page(request: Request, name: str, **ctx):
        request.session.setdefault("csrf", secrets.token_urlsafe(24))
        return templates.TemplateResponse(request, name, {"csrf": request.session["csrf"], "engine": engine_name, **ctx})

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
    def queue(request: Request, status: str = "open", user=Depends(current_user), conn=Depends(get_db)):
        counts = dict(conn.execute("select status, count(*) from tickets group by status").fetchall())
        where, args = ("status in ('drafted', 'needs_human')", ()) if status == "open" else (
            ("status = ?", (status,)) if status in STATUSES else ("1", ()))
        tickets = conn.execute(f"select * from tickets where {where} order by "
                               "case urgency when 'high' then 0 else 1 end, id desc limit 200", args).fetchall()
        stats = conn.execute("""select count(*) total, sum(draft is not null) drafted,
            sum(status = 'sent' and reply = draft) sent_unedited, sum(status = 'sent') sent from tickets""").fetchone()
        gaps = conn.execute("select count(*) from gaps").fetchone()[0]
        return page(request, "queue.html", user=user, tickets=tickets, counts=counts, status=status, stats=stats, gaps=gaps)

    def load_ticket(conn, tid: int):
        t = conn.execute("select * from tickets where id = ?", (tid,)).fetchone()
        if not t:
            raise HTTPException(404, "No such ticket")
        return t

    @app.get("/tickets/{tid}", response_class=HTMLResponse)
    def ticket(request: Request, tid: int, user=Depends(current_user), conn=Depends(get_db)):
        t = load_ticket(conn, tid)
        events = conn.execute("select * from events where ticket_id = ? order by id", (tid,)).fetchall()
        return page(request, "ticket.html", user=user, t=t, events=events, sources=json.loads(t["sources"]),
                    blocked=can_send(user, t), error=request.query_params.get("error"))

    @app.post("/tickets/{tid}/send", dependencies=[Depends(check_csrf)])
    def send_reply(tid: int, reply: str = Form(), user=Depends(current_user), conn=Depends(get_db)):
        t = load_ticket(conn, tid)
        if why := can_send(user, t):
            raise HTTPException(403, why)
        reply = reply.strip().replace("\r\n", "\n")
        if not reply:
            return RedirectResponse(f"/tickets/{tid}?error=The+reply+is+empty", 303)
        # Claim the ticket in the same transaction as the send: a double click or a second agent can't send twice.
        claimed = conn.execute("update tickets set status = 'sent', reply = ?, sent_at = ?, sent_by = ? "
                               "where id = ? and status in ('drafted', 'needs_human')",
                               (reply, store.now(), user["id"], tid)).rowcount
        if not claimed:
            conn.rollback()
            raise HTTPException(409, "Someone already handled this ticket")
        msg = EmailMessage()
        msg["From"], msg["To"] = support_from, t["from_addr"]
        msg["Subject"] = t["subject"] if t["subject"].lower().startswith("re:") else f"Re: {t['subject']}"
        msg["In-Reply-To"] = msg["References"] = t["message_id"]
        msg["Message-ID"] = make_msgid(domain=support_from.split("@")[-1])
        msg.set_content(reply)
        try:
            send(msg)
        except (smtplib.SMTPException, OSError) as e:
            conn.rollback()
            return RedirectResponse(f"/tickets/{tid}?error=Not+sent:+mail+server+error+({type(e).__name__})", 303)
        store.log(conn, tid, user["name"], "sent", {"edited": reply != (t["draft"] or ""), "to": t["from_addr"]})
        conn.commit()
        return RedirectResponse(f"/tickets/{tid}", 303)

    @app.post("/tickets/{tid}/close", dependencies=[Depends(check_csrf)])
    def close(tid: int, note: str = Form(""), user=Depends(current_user), conn=Depends(get_db)):
        if user["role"] not in ("agent", "lead"):
            raise HTTPException(403, "viewers can't close tickets")
        if conn.execute("update tickets set status = 'closed' where id = ? and status != 'sent'", (tid,)).rowcount:
            store.log(conn, tid, user["name"], "closed", {"note": note.strip()} if note.strip() else None)
            conn.commit()
        return RedirectResponse(f"/tickets/{tid}", 303)

    @app.get("/knowledge", response_class=HTMLResponse)
    def knowledge(request: Request, user=Depends(current_user), conn=Depends(get_db)):
        gaps = conn.execute("select g.*, t.from_addr, t.status from gaps g join tickets t on t.id = g.ticket_id "
                            "order by g.id desc").fetchall()
        return page(request, "knowledge.html", user=user, chunks=load_folder(kb_dir), gaps=gaps)

    return app
