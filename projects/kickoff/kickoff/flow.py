"""The client journey, from enquiry to a set-up project:

enquiry → draft proposal (from a package) → sent → viewed → signed (typed name, the exact document's SHA-256, signed
PDF) → deposit paid (Stripe Checkout) → provisioning (project, shared folder, welcome email) → onboarded.

Reminders are queued with the step that makes them relevant and cancelled by the step that makes them moot, so
nobody gets "did you see our proposal?" after signing it.
"""
import hashlib, json, os, re, secrets, time
from datetime import date, datetime, timedelta
from pathlib import Path

from .db import event, money, now, tx

DAY = 86400
REMINDERS = {"not_viewed": 3, "not_signed": 4, "not_paid": 2}  # days
STEPS = ["create_project", "share_folder", "welcome_email"]
TASKS = ["Kickoff call", "Collect access and credentials", "Build", "Test with real data", "Handover and training"]
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)


class Invalid(Exception):
    pass


def day_scale() -> float:
    return float(os.environ.get("DAY_SECONDS_SCALE", 1))  # demo/e2e only: shrinks days to seconds


def public_url() -> str:
    return os.environ.get("PUBLIC_URL", "http://127.0.0.1:8104").rstrip("/")


def email(db, client_id: int | None, kind: str, to: str, subject: str, body: str, delay_days: float = 0, attachment: str | None = None) -> None:
    db.execute("insert into outbox (client_id, kind, to_, subject, body, attachment, due) values (?, ?, ?, ?, ?, ?, ?)",
               (client_id, kind, to, subject, body, attachment, time.time() + delay_days * DAY * day_scale()))


def cancel_reminders(db, client_id: int, *kinds: str) -> None:
    db.execute(f"update outbox set status = 'cancelled' where client_id = ? and status = 'queued' and kind in ({','.join('?' * len(kinds))})", (client_id, *kinds))


def proposal_items(package, chosen: set[str]) -> list[dict]:
    items = [{"name": package["name"], "detail": package["summary"], "cents": package["base_cents"]}]
    for o in json.loads(package["options"]):
        if o["key"] in chosen:
            items.append({"name": o["name"], "detail": "", "cents": o["cents"]})
    return items


def new_proposal(db, client_id: int, items: list[dict], version: int = 1, intro: str = "", deposit_pct: int = 30) -> int:
    return db.execute("""insert into proposals (client_id, version, token, items, total_cents, deposit_pct, valid_until, status, intro, created_at)
                         values (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)""",
                      (client_id, version, secrets.token_urlsafe(24), json.dumps(items), sum(i["cents"] for i in items), deposit_pct,
                       (date.today() + timedelta(days=14)).isoformat(), intro, now())).lastrowid


def enquire(db, *, name: str, email_: str, company: str, brief: str, package_id: int, options: list[str]) -> int:
    name, email_, brief = name.strip()[:120], email_.strip().lower()[:200], brief.strip()[:4000]
    if not name or not EMAIL.match(email_):
        raise Invalid("Please give your name and a valid email address.")
    if len(brief) < 20:
        raise Invalid("Tell us a little more about the project (a few sentences is enough).")
    pkg = db.execute("select * from packages where id = ?", (package_id,)).fetchone()
    if not pkg:
        raise Invalid("Please choose a service.")
    allowed = {o["key"] for o in json.loads(pkg["options"])}
    with tx(db):
        cid = db.execute("insert into clients (name, email, company, brief, package_id, created_at) values (?, ?, ?, ?, ?, ?)",
                         (name, email_, company.strip()[:120], brief, package_id, now())).lastrowid
        new_proposal(db, cid, proposal_items(pkg, set(options) & allowed),
                     intro=f"Hi {name.split()[0]}, thanks for the call notes and the brief. Here is what I propose.")
        event(db, cid, "enquiry", f"{pkg['name']}: {brief[:120]}")
        email(db, cid, "studio_new", os.environ.get("STUDIO_EMAIL", "studio@example.com"), f"New enquiry: {company or name}",
              f"{name} <{email_}> asked about {pkg['name']}.\n\n{brief}\n\nA draft proposal is ready: {public_url()}/clients/{cid}")
    return cid


def edit_proposal(db, pid: int, items: list[dict], deposit_pct: int, intro: str) -> None:
    p = db.execute("select * from proposals where id = ?", (pid,)).fetchone()
    if p["status"] != "draft":
        raise Invalid("Only drafts can be edited. Withdraw the sent proposal to make a new version.")
    items = [i for i in items if i["name"].strip()]
    if not items or any(i["cents"] < 0 for i in items):
        raise Invalid("A proposal needs at least one line, and no negative prices.")
    if not 0 <= deposit_pct <= 100:
        raise Invalid("The deposit is a percentage between 0 and 100.")
    db.execute("update proposals set items = ?, total_cents = ?, deposit_pct = ?, intro = ? where id = ?",
               (json.dumps(items), sum(i["cents"] for i in items), deposit_pct, intro[:2000], pid))


def send_proposal(db, pid: int, who: str) -> None:
    with tx(db):
        p = db.execute("select * from proposals where id = ?", (pid,)).fetchone()
        if p["status"] != "draft":
            raise Invalid("This proposal was already sent.")
        c = db.execute("select * from clients where id = ?", (p["client_id"],)).fetchone()
        valid = (date.today() + timedelta(days=14)).isoformat()
        db.execute("update proposals set status = 'sent', sent_at = ?, valid_until = ? where id = ?", (now(), valid, pid))
        db.execute("update clients set stage = 'proposal_sent' where id = ?", (c["id"],))
        link = f"{public_url()}/p/{p['token']}"
        email(db, c["id"], "proposal", c["email"], f"Your proposal: {money(p['total_cents'])}",
              f"Hi {c['name'].split()[0]},\n\nhere is the proposal we talked about. You can read it, ask questions and sign it online:\n{link}\n\n"
              f"It's valid until {valid}.\n\nBasar")
        email(db, c["id"], "not_viewed", c["email"], "Did the proposal reach you?",
              f"Hi {c['name'].split()[0]}, just checking the proposal arrived: {link}", delay_days=REMINDERS["not_viewed"])
        event(db, c["id"], "sent", f"version {p['version']} by {who}")


def withdraw(db, pid: int, who: str) -> int:
    with tx(db):
        p = db.execute("select * from proposals where id = ?", (pid,)).fetchone()
        if p["status"] not in ("sent", "viewed", "expired"):
            raise Invalid("Only a sent, unsigned proposal can be withdrawn.")
        db.execute("update proposals set status = 'withdrawn' where id = ?", (pid,))
        cancel_reminders(db, p["client_id"], "not_viewed", "not_signed")
        new = new_proposal(db, p["client_id"], json.loads(p["items"]), p["version"] + 1, p["intro"], p["deposit_pct"])
        db.execute("update clients set stage = 'enquiry' where id = ?", (p["client_id"],))
        event(db, p["client_id"], "withdrawn", f"version {p['version']} withdrawn by {who}; version {p['version'] + 1} drafted")
    return new


def contract_text(p, c, pkg) -> str:
    """The exact text that is signed. Its SHA-256 is shown to the signer, stored with the signature and printed in the PDF."""
    items = json.loads(p["items"])
    deposit = p["total_cents"] * p["deposit_pct"] // 100
    lines = [f"PROPOSAL AND AGREEMENT (version {p['version']})", "",
             f"Client: {c['name']}{', ' + c['company'] if c['company'] else ''} <{c['email']}>",
             f"Provider: {os.environ.get('STUDIO_NAME', 'Basar Tuncay, automation & integrations')}", "", "SCOPE AND PRICE"]
    lines += [f"- {i['name']}: {money(i['cents'])}" + (f" ({i['detail']})" if i["detail"] else "") for i in items]
    lines += [f"Total: {money(p['total_cents'])} (VAT added where applicable)",
              f"Deposit due on signing: {p['deposit_pct']}% = {money(deposit)}; the rest on handover.", "",
              f"TIMELINE: about {pkg['weeks'] if pkg else '?'} week(s) from the kickoff call.", "", "TERMS",
              "1. Work starts after the deposit is paid.",
              "2. Changes to the scope are agreed in writing and priced before they're built.",
              "3. The client gets full ownership of the code and configuration on final payment.",
              "4. Access the client provides is used only for this project and removed on handover.",
              "5. Either side can end the agreement in writing; work done until then is paid.",
              "", f"Valid until {p['valid_until']}."]
    return "\n".join(lines)


def view(db, token: str) -> tuple:
    p = db.execute("select * from proposals where token = ?", (token,)).fetchone()
    if not p or p["status"] in ("draft", "withdrawn"):
        return None, None
    c = db.execute("select * from clients where id = ?", (p["client_id"],)).fetchone()
    if p["status"] in ("sent", "viewed") and p["valid_until"] < date.today().isoformat():
        db.execute("update proposals set status = 'expired' where id = ?", (p["id"],))
        event(db, c["id"], "expired", f"version {p['version']}")
    elif p["status"] == "sent":
        with tx(db):
            db.execute("update proposals set status = 'viewed', viewed_at = ? where id = ? and status = 'sent'", (now(), p["id"]))
            db.execute("update clients set stage = 'viewed' where id = ? and stage = 'proposal_sent'", (c["id"],))
            cancel_reminders(db, c["id"], "not_viewed")
            email(db, c["id"], "not_signed", c["email"], "Any questions about the proposal?",
                  f"Hi {c['name'].split()[0]}, happy to go through the proposal on a short call if anything is unclear: {public_url()}/p/{token}",
                  delay_days=REMINDERS["not_signed"])
            event(db, c["id"], "viewed", f"version {p['version']}")
    return db.execute("select * from proposals where id = ?", (p["id"],)).fetchone(), c


def sign(db, token: str, *, typed_name: str, agreed: bool, shown_sha256: str, ip: str, user_agent: str, stripe, contracts_dir: Path) -> dict:
    p, c = view(db, token)
    if not p:
        raise Invalid("This proposal link isn't valid any more. Ask us for the current one.")
    if p["status"] == "expired":
        raise Invalid("This proposal has expired. Reply to our email and we'll send a fresh one.")
    if p["status"] == "signed":
        raise Invalid("This proposal is already signed.")
    pkg = db.execute("select * from packages where id = ?", (c["package_id"],)).fetchone()
    text = contract_text(p, c, pkg)
    digest = hashlib.sha256(text.encode()).hexdigest()
    if not secrets.compare_digest(digest, shown_sha256 or ""):
        raise Invalid("The proposal changed while you had it open. Please reload and read it again before signing.")
    if not agreed or len(typed_name.strip()) < 3:
        raise Invalid("Type your full name and tick the box to sign.")
    record = {"name": typed_name.strip()[:120], "email": c["email"], "at": now() + " UTC", "ip": ip, "user_agent": user_agent[:300], "sha256": digest}
    contracts_dir.mkdir(parents=True, exist_ok=True)
    pdf = contracts_dir / f"client-{c['id']}-v{p['version']}-{digest[:12]}.pdf"
    from .pdf import render
    render(text, record, pdf)
    deposit = p["total_cents"] * p["deposit_pct"] // 100
    with tx(db):
        if not db.execute("update proposals set status = 'signed', signed_at = ?, signature = ?, doc_sha256 = ?, pdf = ? where id = ? and status in ('sent', 'viewed')",
                          (now(), json.dumps(record), digest, str(pdf), p["id"])).rowcount:
            raise Invalid("This proposal is already signed.")
        db.execute("update clients set stage = 'signed' where id = ?", (c["id"],))
        cancel_reminders(db, c["id"], "not_viewed", "not_signed")
        event(db, c["id"], "signed", f"by {record['name']} from {ip}, sha256 {digest[:16]}…")
    pay_url = None
    if deposit:
        pay_url = start_payment(db, p["id"], stripe)
    with tx(db):
        email(db, c["id"], "signed_copy", c["email"], "Signed: your copy of the agreement",
              f"Hi {c['name'].split()[0]},\n\nthanks for signing. Your copy is attached (document fingerprint {digest[:16]}…).\n\n"
              + (f"To get started, the deposit of {money(deposit)} can be paid here:\n{pay_url}\n" if pay_url else "") + "\nBasar", attachment=str(pdf))
        if pay_url:
            email(db, c["id"], "not_paid", c["email"], "Deposit reminder", f"Hi {c['name'].split()[0]}, a gentle reminder: the deposit link is {pay_url}",
                  delay_days=REMINDERS["not_paid"])
    if not deposit:
        mark_paid(db, p["id"], "no deposit")
    return {"pay_url": pay_url, "sha256": digest}


def start_payment(db, pid: int, stripe) -> str:
    p = db.execute("select * from proposals where id = ?", (pid,)).fetchone()
    c = db.execute("select * from clients where id = ?", (p["client_id"],)).fetchone()
    existing = db.execute("select url from payments where proposal_id = ? and status = 'open'", (pid,)).fetchone()
    if existing:
        return existing[0]
    deposit = p["total_cents"] * p["deposit_pct"] // 100
    s = stripe.checkout(proposal_id=pid, amount_cents=deposit, name=f"Deposit: {c['company'] or c['name']} (proposal v{p['version']})",
                        email=c["email"], success_url=f"{public_url()}/p/{p['token']}?paid=1", cancel_url=f"{public_url()}/p/{p['token']}")
    db.execute("insert or ignore into payments (proposal_id, session_id, url, amount_cents, created_at) values (?, ?, ?, ?, ?)",
               (pid, s["id"], s["url"], deposit, now()))
    return s["url"]


def mark_paid(db, pid: int, ref: str) -> bool:
    with tx(db):
        p = db.execute("select * from proposals where id = ?", (pid,)).fetchone()
        c = db.execute("select * from clients where id = ?", (p["client_id"],)).fetchone()
        if c["stage"] in ("paid", "onboarded"):
            return False
        db.execute("update payments set status = 'paid', paid_at = ? where proposal_id = ? and status = 'open'", (now(), pid))
        db.execute("update clients set stage = 'paid' where id = ?", (c["id"],))
        cancel_reminders(db, c["id"], "not_paid")
        db.executemany("insert or ignore into steps (client_id, name, position) values (?, ?, ?)", [(c["id"], s, i) for i, s in enumerate(STEPS)])
        event(db, c["id"], "paid", ref)
    return True


def handle_stripe_event(db, ev: dict) -> str:
    if db.execute("select 1 from webhook_events where id = ?", (ev["id"],)).fetchone():
        return "duplicate"
    db.execute("insert into webhook_events values (?, ?)", (ev["id"], now()))
    if ev["type"] != "checkout.session.completed":
        return "ignored"
    s = ev["data"]["object"]
    if s.get("payment_status") != "paid":
        return "not paid yet"
    pay = db.execute("select * from payments where session_id = ?", (s["id"],)).fetchone()
    if not pay or s.get("amount_total") != pay["amount_cents"]:
        return "unknown session"
    return "paid" if mark_paid(db, pay["proposal_id"], f"Stripe {s['id']}, {money(s['amount_total'])}") else "already paid"


def provision(db, projects, storage, max_attempts: int = 5) -> int:
    """Run due provisioning steps in order. Each step is idempotent, so a retry after a timeout never duplicates."""
    done = 0
    for cid in [r[0] for r in db.execute("select distinct client_id from steps where status = 'pending' and next_at <= ?", (time.time(),))]:
        c = db.execute("select * from clients where id = ?", (cid,)).fetchone()
        results = {r["name"]: json.loads(r["result"]) for r in db.execute("select * from steps where client_id = ? and status = 'done'", (cid,))}
        for st in db.execute("select * from steps where client_id = ? order by position", (cid,)).fetchall():
            if st["status"] == "done":
                continue
            if st["status"] == "failed" or st["next_at"] > time.time():
                break  # later steps wait for this one
            try:
                if st["name"] == "create_project":
                    out = projects.ensure_project(f"client-{cid}", f"{c['company'] or c['name']}: {db.execute('select name from packages where id = ?', (c['package_id'],)).fetchone()[0]}", TASKS)
                elif st["name"] == "share_folder":
                    out = storage.ensure_shared_folder(f"{c['company'] or c['name']} (client {cid})", c["email"])
                else:
                    kick = os.environ.get("KICKOFF_BOOKING_URL", "https://cal.example.com/basar/kickoff")
                    with tx(db):
                        email(db, cid, "welcome", c["email"], "Welcome aboard: next steps",
                              f"Hi {c['name'].split()[0]},\n\nthe deposit arrived, thank you. Everything is set up:\n\n"
                              f"- Book the kickoff call: {kick}\n- Shared folder for files and access details: {results['share_folder']['url']}\n"
                              f"- Project board, so you can follow progress: {results['create_project']['url']}\n\nTalk soon,\nBasar")
                    out = {"queued": True}
            except Exception as e:  # noqa: BLE001 - any failure is retried with backoff, then shown in the panel
                n = st["attempts"] + 1
                status = "failed" if n >= max_attempts else "pending"
                db.execute("update steps set attempts = ?, status = ?, error = ?, next_at = ? where id = ?",
                           (n, status, str(e)[:300], time.time() + min(2 ** n, 300) * float(os.environ.get("BACKOFF_SCALE", 1)), st["id"]))
                event(db, cid, "step_failed" if status == "failed" else "step_retry", f"{st['name']}: {e}")
                break
            db.execute("update steps set status = 'done', result = ?, error = null, done_at = ?, attempts = attempts + 1 where id = ?", (json.dumps(out), now(), st["id"]))
            results[st["name"]] = out
            event(db, cid, "step_done", st["name"].replace("_", " "))
            done += 1
        if db.execute("select count(*) from steps where client_id = ? and status != 'done'", (cid,)).fetchone()[0] == 0:
            db.execute("update clients set stage = 'onboarded' where id = ? and stage = 'paid'", (cid,))
            event(db, cid, "onboarded", "project, folder and welcome email done")
    return done


def retry_step(db, step_id: int) -> None:
    db.execute("update steps set status = 'pending', attempts = 0, next_at = 0 where id = ? and status = 'failed'", (step_id,))
