"""Outreach: qualified leads get a short email sequence, replies stop it.

- Steps after the first are sent in the same thread (In-Reply-To/References, "Re:"), like a person following up.
- Only inside the sending window (weekdays, office hours, in the configured time zone) and under a daily cap.
- Before every email the lead's status and the suppression list are checked again: an unsubscribe, a bounce or a
  reply that arrived a minute ago wins over the schedule.
- Every email has a one-click unsubscribe link and List-Unsubscribe headers (RFC 8058).
- Replies are read from the sales inbox over IMAP, matched to the lead by thread headers (or sender), and sorted:
  bounce, auto-reply, unsubscribe, not interested, or a human reply worth a call. Only the last goes to the CRM.
"""
import base64, email, email.policy, hashlib, hmac, imaplib, json, os, re, smtplib, time
from collections import defaultdict
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from zoneinfo import ZoneInfo

import httpx

from .db import event, now, set_status, settings

LEASE = 600  # seconds an enrollment is held while its email is being sent


def unsubscribe_token(lead_id: int) -> str:
    sig = hmac.new(os.environ["SECRET_KEY"].encode(), f"unsub:{lead_id}".encode(), hashlib.sha256).digest()[:12]
    return f"{lead_id}.{base64.urlsafe_b64encode(sig).decode().rstrip('=')}"


def check_token(token: str) -> int | None:
    lead_id = token.split(".", 1)[0]
    return int(lead_id) if lead_id.isdigit() and hmac.compare_digest(unsubscribe_token(int(lead_id)), token) else None


def suppress(db, value: str, reason: str, by: str) -> None:
    db.execute("insert into suppressions values (?, ?, ?, ?) on conflict(value) do nothing", (value.lower(), reason, now(), by))


def enroll(db, lead_id: int, sequence_id: int | None = None, by: str = "auto") -> bool:
    seq = sequence_id or db.execute("select id from sequences where active = 1 order by id limit 1").fetchone()[0]
    cur = db.execute("insert or ignore into enrollments (lead_id, sequence_id, next_at, created_at) values (?, ?, ?, ?)", (lead_id, seq, time.time(), now()))
    if cur.rowcount:
        set_status(db, lead_id, "in_sequence", f"enrolled by {by}")
    return bool(cur.rowcount)


def enroll_qualified(db) -> int:
    if not settings(db)["auto_enroll"]:
        return 0
    return sum(enroll(db, r["id"]) for r in db.execute("select id from leads where status = 'qualified' order by score desc, id").fetchall())


def in_window(s: dict, at: datetime | None = None) -> bool:
    w = s["send_window"]
    t = at or datetime.now(ZoneInfo(w["timezone"]))
    return t.weekday() in w["days"] and w["from"] <= t.strftime("%H:%M") < w["to"]


def render(template: str, lead, enrichment: dict | None) -> str:
    values = defaultdict(str, first_name=lead["first_name"] or "there", company=lead["company"] or "your team",
                         tech=((enrichment or {}).get("tech") or ["your shop platform"])[0])
    return template.format_map(values)


class Mailer:
    def __init__(self):
        e = os.environ
        self.host, self.port = e["SMTP_HOST"], int(e.get("SMTP_PORT", 587))
        self.user, self.password = e.get("SMTP_USER"), e.get("SMTP_PASSWORD")
        self.starttls = e.get("SMTP_STARTTLS", "true").lower() == "true"
        self.sender = e["FROM_EMAIL"]

    def send(self, msg: EmailMessage) -> None:
        with smtplib.SMTP(self.host, self.port, timeout=20) as s:
            if self.starttls:
                s.starttls()
            if self.user:
                s.login(self.user, self.password)
            s.send_message(msg)


def send_due(db, mailer: Mailer, force_window: bool = False) -> int:
    s = settings(db)
    if not (force_window or os.environ.get("IGNORE_SEND_WINDOW", "false").lower() == "true") and not in_window(s):
        return 0
    tz = ZoneInfo(s["send_window"]["timezone"])
    today = datetime.now(tz).strftime("%Y-%m-%d")
    sent = 0
    public = os.environ.get("PUBLIC_URL", "http://127.0.0.1:8100").rstrip("/")
    for en in db.execute("select * from enrollments where status = 'active' and next_at <= ? order by next_at limit 50", (time.time(),)).fetchall():
        used = (db.execute("select n from sent_counts where day = ?", (today,)).fetchone() or [0])[0]
        if used >= s["daily_cap"]:
            break
        if not db.execute("update enrollments set next_at = ? where id = ? and next_at = ? and status = 'active'", (time.time() + LEASE, en["id"], en["next_at"])).rowcount:
            continue  # another worker has it
        lead = db.execute("select * from leads where id = ?", (en["lead_id"],)).fetchone()
        steps = json.loads(db.execute("select steps from sequences where id = ?", (en["sequence_id"],)).fetchone()[0])
        blocked = db.execute("select reason from suppressions where value in (?, ?)", (lead["email"], lead["domain"] or "")).fetchone()
        if lead["status"] != "in_sequence" or blocked:
            db.execute("update enrollments set status = 'stopped', stop_reason = ? where id = ?",
                       (f"suppressed: {blocked[0]}" if blocked else f"lead is {lead['status']}", en["id"]))
            continue
        step = steps[en["step"]]
        enrichment = json.loads(lead["enrichment"]) if lead["enrichment"] else None
        thread = db.execute("select message_id, subject from emails where enrollment_id = ? and direction = 'out' order by id", (en["id"],)).fetchall()
        msg = EmailMessage()
        msg["From"] = formataddr((s["sender_name"], mailer.sender))
        msg["To"] = formataddr((f"{lead['first_name'] or ''} {lead['last_name'] or ''}".strip(), lead["email"]))
        msg["Subject"] = render(step["subject"], lead, enrichment) if not thread else "Re: " + thread[0]["subject"]
        msg["Message-ID"] = make_msgid(domain=mailer.sender.split("@")[1])
        if thread:
            msg["In-Reply-To"] = thread[-1]["message_id"]
            msg["References"] = " ".join(t["message_id"] for t in thread)
        link = f"{public}/u/{unsubscribe_token(lead['id'])}"
        msg["List-Unsubscribe"] = f"<{link}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        body = render(step["body"], lead, enrichment)
        msg.set_content(f"{body}\n\n--\nNot relevant? One click and you won't hear from me again: {link}")
        try:
            mailer.send(msg)
        except (smtplib.SMTPException, OSError) as e:
            db.execute("update enrollments set next_at = ? where id = ?", (time.time() + 300, en["id"]))
            event(db, lead["id"], "send_retry", f"step {en['step'] + 1}: {e}")
            continue
        db.execute("begin immediate")
        try:
            db.execute("""insert into emails (lead_id, enrollment_id, step, direction, message_id, in_reply_to, subject, body, at)
                          values (?, ?, ?, 'out', ?, ?, ?, ?, ?)""",
                       (lead["id"], en["id"], en["step"], msg["Message-ID"], msg["In-Reply-To"], str(msg["Subject"]), body, now()))
            db.execute("insert into sent_counts values (?, 1) on conflict(day) do update set n = n + 1", (today,))
            if en["step"] + 1 < len(steps):
                nxt = time.time() + steps[en["step"] + 1]["delay_days"] * 86400 * float(os.environ.get("DAY_SECONDS_SCALE", 1))
                db.execute("update enrollments set step = step + 1, next_at = ? where id = ?", (nxt, en["id"]))
            else:
                db.execute("update enrollments set status = 'finished', step = step + 1 where id = ?", (en["id"],))
                db.execute("update leads set status = 'finished', updated_at = ? where id = ? and status = 'in_sequence'", (now(), lead["id"]))
            event(db, lead["id"], "email_sent", f"step {en['step'] + 1}: {msg['Subject']}")
            db.execute("commit")
        except BaseException:
            db.execute("rollback")
            raise
        sent += 1
    return sent


# ---- replies ----

OOO = re.compile(r"out of (the )?office|abwesen|automatic reply|automatische antwort|auto.?reply|im urlaub|on vacation|on holiday", re.I)
UNSUB = re.compile(r"unsubscribe|remove me|take me off|stop emailing|abmelden|austragen|keine (weiteren )?e-?mails", re.I)
NO = re.compile(r"not interested|no thanks|no thank you|kein interesse|nicht interessiert|we('re| are) (all )?set|already have", re.I)
DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})|(\d{1,2})\.(\d{1,2})\.(\d{4})")


def body_text(msg) -> str:
    part = msg.get_body(preferencelist=("plain", "html")) if msg.is_multipart() else msg
    text = part.get_content() if part else ""
    lines = []
    for line in text.splitlines():  # only the new part of the reply, not the quoted thread
        if line.startswith(">") or re.match(r"^(On .+ wrote:|Am .+ schrieb .+:)$", line.strip()):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def classify(msg) -> tuple[str, str]:
    sender = parseaddr(msg.get("From", ""))[1].lower()
    if sender.startswith(("mailer-daemon", "postmaster")) or msg.get_content_type() == "multipart/report":
        return "bounce", ""
    text, subject = body_text(msg), msg.get("Subject", "")
    auto = msg.get("Auto-Submitted", "no").lower() != "no" or msg.get("X-Autoreply") or msg.get("X-Autorespond")
    if auto or OOO.search(subject) or OOO.search(text[:300]):
        return "auto_reply", text
    if UNSUB.search(text):
        return "unsubscribe", text
    if NO.search(text):
        return "not_interested", text
    return "interested", text


def return_date(text: str) -> datetime | None:
    for m in DATE.finditer(text):
        try:
            d = datetime(int(m[1]), int(m[2]), int(m[3])) if m[1] else datetime(int(m[6]), int(m[5]), int(m[4]))
        except ValueError:
            continue
        if d > datetime.now():
            return d
    return None


def find_lead(db, msg, kind: str):
    refs = re.findall(r"<[^>]+>", f"{msg.get('In-Reply-To', '')} {msg.get('References', '')}")
    if kind == "bounce":  # the bounce quotes our original message: find its Message-ID or the failed recipient
        raw = msg.as_string()
        refs += re.findall(r"Message-ID:\s*(<[^>]+>)", raw, re.I)
        rcpt = re.search(r"Final-Recipient:\s*rfc822;\s*([^\s]+)", raw, re.I)
        if rcpt:
            hit = db.execute("select * from leads where email = ?", (rcpt[1].lower(),)).fetchone()
            if hit:
                return hit
    for ref in refs:
        hit = db.execute("select l.* from emails e join leads l on l.id = e.lead_id where e.message_id = ?", (ref,)).fetchone()
        if hit:
            return hit
    return db.execute("select * from leads where email = ?", (parseaddr(msg.get("From", ""))[1].lower(),)).fetchone()


def handle_reply(db, msg) -> str | None:
    if msg.get("Message-ID") and db.execute("select 1 from emails where message_id = ?", (msg["Message-ID"],)).fetchone():
        return None  # already seen
    kind, text = classify(msg)
    lead = find_lead(db, msg, kind)
    if not lead:
        return None
    db.execute("insert into emails (lead_id, direction, message_id, in_reply_to, subject, body, at, classification) values (?, 'in', ?, ?, ?, ?, ?, ?)",
               (lead["id"], msg.get("Message-ID"), msg.get("In-Reply-To"), msg.get("Subject", "")[:300], text[:5000], now(), kind))
    active = db.execute("select id from enrollments where lead_id = ? and status = 'active'", (lead["id"],)).fetchone()
    stop = lambda why: active and db.execute("update enrollments set status = 'stopped', stop_reason = ? where id = ?", (why, active[0]))
    if kind == "auto_reply":
        back = return_date(text) or datetime.now() + timedelta(days=7)
        if active:
            db.execute("update enrollments set next_at = ? where id = ?", (back.timestamp() + 86400, active[0]))
        event(db, lead["id"], "auto_reply", f"away until {back:%Y-%m-%d}; next email moved to the day after")
    elif kind == "bounce":
        stop("bounced")
        suppress(db, lead["email"], "bounced", "system")
        set_status(db, lead["id"], "bounced", "the address bounced")
    elif kind == "unsubscribe":
        stop("asked to stop")
        suppress(db, lead["email"], "asked to stop by email", "system")
        set_status(db, lead["id"], "unsubscribed", "asked to stop in a reply")
    elif kind == "not_interested":
        stop("not interested")
        suppress(db, lead["email"], "not interested", "system")
        set_status(db, lead["id"], "not_interested", text[:120])
    else:
        stop("replied")
        set_status(db, lead["id"], "interested", text[:120])
    return kind


def poll_inbox(db) -> list[str]:
    e = os.environ
    imap = (imaplib.IMAP4_SSL if e.get("IMAP_SSL", "true").lower() == "true" else imaplib.IMAP4)(e["IMAP_HOST"], int(e.get("IMAP_PORT", 993)))
    out = []
    try:
        imap.login(e["IMAP_USER"], e["IMAP_PASSWORD"])
        imap.select("INBOX")
        _, ids = imap.search(None, "UNSEEN")
        for i in ids[0].split():
            _, data = imap.fetch(i, "(RFC822)")
            msg = email.message_from_bytes(data[0][1], policy=email.policy.default)
            kind = handle_reply(db, msg)
            if kind:
                out.append(kind)
            imap.store(i, "+FLAGS", "\\Seen")
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return out


# ---- CRM: only people who replied with interest ----

def push_interested(db, http: httpx.Client | None = None) -> int:
    base, token = os.environ["CRM_URL"].rstrip("/"), os.environ["CRM_TOKEN"]
    http = http or httpx.Client(timeout=15)
    h = {"Authorization": f"Bearer {token}"}
    pushed = 0
    for lead in db.execute("select * from leads where status = 'interested' and crm_id is null").fetchall():
        reply = db.execute("select body from emails where lead_id = ? and direction = 'in' order by id desc limit 1", (lead["id"],)).fetchone()
        props = {"email": lead["email"], "firstname": lead["first_name"], "lastname": lead["last_name"], "company": lead["company"],
                 "jobtitle": lead["title"], "lead_score": str(lead["score"] or ""), "hs_lead_status": "IN_PROGRESS"}
        try:
            r = http.post(f"{base}/crm/v3/objects/contacts", json={"properties": props}, headers=h)
            if r.status_code == 409:  # already in the CRM: attach the deal to the existing contact
                cid = re.search(r"Existing ID: (\d+)", r.json().get("message", ""))[1]
            elif r.status_code >= 400:
                raise httpx.HTTPStatusError(str(r.status_code), request=r.request, response=r)
            else:
                cid = r.json()["id"]
            d = http.post(f"{base}/crm/v3/objects/deals", headers=h, json={
                "properties": {"dealname": f"{lead['company'] or lead['email']}: integration work", "pipeline": "default", "dealstage": "appointmentscheduled",
                               "description": f"Replied: {(reply[0] if reply else '')[:500]}"},
                "associations": [{"to": {"id": cid}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 3}]}]})
            d.raise_for_status()
        except (httpx.HTTPError, TypeError) as e:
            event(db, lead["id"], "crm_retry", str(e)[:200])
            continue
        db.execute("update leads set crm_id = ?, updated_at = ? where id = ?", (cid, now(), lead["id"]))
        event(db, lead["id"], "crm", f"contact {cid}, deal {d.json()['id']}")
        pushed += 1
    return pushed
