"""The accounting system's side: mirror open invoices, push the payments we matched, and send dunning letters.

The local open amount is the accounting system's open amount minus our matches it hasn't received yet, so a sync
between matching and booking can never make an invoice look open again and get paid twice."""
import json, os, smtplib, time
from datetime import date, timedelta
from email.message import EmailMessage

import httpx

from .db import euro, now, settings, tx


class Accounting:
    def __init__(self, http: httpx.Client | None = None):
        self.base = os.environ["ACCOUNTING_URL"].rstrip("/")
        self.http = http or httpx.Client(timeout=20)
        self.h = {"Authorization": "Bearer " + os.environ["ACCOUNTING_TOKEN"]}

    def open_invoices(self) -> list[dict]:
        out, page = [], 1
        while page:
            r = self.http.get(f"{self.base}/v1/invoices", params={"status": "open", "page": page}, headers=self.h)
            r.raise_for_status()
            body = r.json()
            out += body["data"]
            page = body.get("next_page")
        return out

    def add_payment(self, invoice_id: str, **payment) -> httpx.Response:
        return self.http.post(f"{self.base}/v1/invoices/{invoice_id}/payments", json=payment, headers=self.h)


def sync(db, api: Accounting) -> dict:
    remote = {i["id"]: i for i in api.open_invoices()}
    with tx(db):
        for i in remote.values():
            pending = db.execute("select coalesce(sum(amount_cents + writeoff_cents), 0) from matches where invoice_id = ? and booked = 0", (i["id"],)).fetchone()[0]
            left = max(0, i["open_cents"] - pending)
            db.execute("""insert into invoices (id, number, customer, customer_no, email, issued, due, total_cents, open_cents, status, synced_at)
                          values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) on conflict(id) do update set open_cents = excluded.open_cents, status = excluded.status,
                          due = excluded.due, email = excluded.email, synced_at = excluded.synced_at""",
                       (i["id"], i["number"], i["customer"], i.get("customer_no"), i.get("email"), i["issued"], i["due"], i["total_cents"], left,
                        "open" if left == i["total_cents"] else ("paid" if left == 0 else "partial"), now()))
        gone = [r["id"] for r in db.execute("select id from invoices where open_cents > 0").fetchall() if r["id"] not in remote]
        for g in gone:  # paid or cancelled in the accounting system itself
            db.execute("update invoices set open_cents = 0, status = 'closed elsewhere' where id = ?", (g,))
    return {"open": len(remote), "closed_elsewhere": len(gone)}


def push(db, api: Accounting) -> int:
    done = 0
    for m in db.execute("select m.*, t.booked day from matches m join transactions t on t.id = m.transaction_id where m.booked = 0").fetchall():
        try:
            r = api.add_payment(m["invoice_id"], amount_cents=m["amount_cents"], date=m["day"], external_id=f"paymatch-{m['id']}",
                                writeoff_cents=m["writeoff_cents"], writeoff_reason=m["writeoff_reason"])
        except httpx.TransportError as e:
            db.execute("update matches set booking_error = ? where id = ?", (str(e)[:200], m["id"]))
            break
        if r.status_code in (200, 201, 409):  # 409: already booked under this external ID (we retried after a timeout)
            db.execute("update matches set booked = 1, booking_error = null where id = ?", (m["id"],))
            done += 1
        elif r.status_code == 429 or r.status_code >= 500:
            db.execute("update matches set booking_error = ? where id = ?", (f"{r.status_code}, will retry", m["id"]))
            break
        else:
            db.execute("update matches set booking_error = ? where id = ?", (f"{r.status_code}: {r.text[:200]}", m["id"]))
    return done


LETTERS = {
    1: ("Friendly reminder: invoice {number}", "Hello {customer},\n\nour records show invoice {number} from {issued} ({open}) as still open; it was due on {due}. "
        "If you've already paid, thank you, and please ignore this.\n\nKind regards\n{company}"),
    2: ("Second reminder: invoice {number}", "Hello {customer},\n\ninvoice {number} ({open}) is now {days} days overdue. Please transfer the amount "
        "within 7 days, quoting {number}.\n\nKind regards\n{company}"),
    3: ("Final reminder: invoice {number}", "Hello {customer},\n\ndespite two reminders, invoice {number} is {days} days overdue. Please pay {open} plus a "
        "reminder fee of {fee} within 7 days. After that we will hand the claim over.\n\n{company}"),
}


def dunning(db, today: date | None = None) -> list[str]:
    """Next reminder level for overdue invoices, at most one level per invoice per run and 7 days apart. Invoices
    that a payment waiting for review might settle are held back: nobody gets a reminder for money already sent."""
    s, today = settings(db), today or date.today()
    latest = db.execute("select max(booked) from transactions").fetchone()[0]
    if not latest or latest < (today - timedelta(days=3)).isoformat():
        return []  # bank data is stale: a reminder now could ask for money that has already arrived
    held = set()
    for t in db.execute("select suggestions from transactions where status = 'suggested'"):
        for c in json.loads(t["suggestions"] or "[]"):
            held.update(c["invoices"])
    sent = []
    for inv in db.execute("select * from invoices where open_cents > 0 and due < ? and email is not null order by due", (today.isoformat(),)).fetchall():
        if inv["id"] in held:
            continue
        days = (today - date.fromisoformat(inv["due"])).days
        last = db.execute("select level, at from dunning where invoice_id = ? order by level desc limit 1", (inv["id"],)).fetchone()
        level = (last["level"] if last else 0) + 1
        if level > 3 or days < s["dunning_days"][level - 1] or (last and date.fromisoformat(last["at"][:10]) > today - timedelta(days=7)):
            continue
        fee = s["dunning_fee_cents"] if level == 3 else 0
        subject, body = LETTERS[level]
        vals = {"number": inv["number"], "customer": inv["customer"], "issued": inv["issued"], "due": inv["due"], "open": euro(inv["open_cents"]),
                "days": days, "fee": euro(fee), "company": s["company"]}
        with tx(db):
            db.execute("insert into dunning (invoice_id, level, at, fee_cents) values (?, ?, ?, ?)", (inv["id"], level, today.isoformat() + " " + now()[11:], fee))
            db.execute("insert into outbox (kind, to_, subject, body, invoice_id) values (?, ?, ?, ?, ?)",
                       (f"dunning_{level}", inv["email"], subject.format(**vals), body.format(**vals), inv["id"]))
        sent.append(f"{inv['number']}: level {level}")
    return sent


def send_mail(db) -> int:
    e, n = os.environ, 0
    for m in db.execute("select * from outbox where status = 'queued' and attempts < 5 order by id limit 30").fetchall():
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = e["FROM_EMAIL"], m["to_"], m["subject"]
        msg.set_content(m["body"])
        try:
            with smtplib.SMTP(e["SMTP_HOST"], int(e.get("SMTP_PORT", 587)), timeout=20) as s:
                if e.get("SMTP_STARTTLS", "true").lower() == "true":
                    s.starttls()
                if e.get("SMTP_USER"):
                    s.login(e["SMTP_USER"], e["SMTP_PASSWORD"])
                s.send_message(msg)
        except (smtplib.SMTPException, OSError) as err:
            db.execute("update outbox set attempts = attempts + 1, error = ?, status = case when attempts + 1 >= 5 then 'failed' else 'queued' end where id = ?",
                       (str(err)[:200], m["id"]))
            continue
        db.execute("update outbox set status = 'sent', sent_at = ? where id = ?", (now(), m["id"]))
        n += 1
    return n
