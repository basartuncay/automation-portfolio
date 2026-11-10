"""Poll the support inbox, triage each email, draft a grounded reply when the docs cover it, file it in the panel DB.

An email is marked \\Seen only after it was stored, so an outage means a retry, never a loss. Message-ID makes
re-processing a no-op. Nothing is sent from here: every reply waits for a person in the panel.
"""
import email, hashlib, imaplib, json, logging, os, time
from email import policy
from email.utils import parseaddr
from pathlib import Path

from doc_qa.core import BM25, load_folder
from email_triage.core import Category, Urgency, route

from . import db as store
from .engine import TransientError, make_engine, search

log = logging.getLogger("support-intake")
BOT = "Intake bot"


def parse(raw: bytes) -> dict:
    msg = email.message_from_bytes(raw, policy=policy.default)
    name, addr = parseaddr(str(msg.get("From", "")))
    body = msg.get_body(preferencelist=("plain", "html"))
    text = body.get_content() if body else ""
    mid = str(msg.get("Message-ID") or "") or "<sha-" + hashlib.sha256(raw).hexdigest()[:24] + ">"
    return {"message_id": mid.strip(), "from": addr.lower(), "from_name": name, "subject": str(msg.get("Subject") or ""),
            "date": str(msg.get("Date") or ""), "body": text}


def handle(conn, engine, index: BM25, mail: dict) -> str | None:
    """Store one email as a ticket. Returns the new status, or None if it was already stored."""
    if conn.execute("select 1 from tickets where message_id = ?", (mail["message_id"],)).fetchone():
        return None
    t, problem = engine.triage(mail)  # TransientError propagates: the caller leaves the email unread
    draft, sources, reason, route_to = None, [], None, None
    if t is None:
        status, reason = "needs_human", f"could not triage automatically: {problem}"
    elif t.suspected_injection:
        status, reason = "needs_human", "possible prompt injection: no draft was written, read it yourself"
    elif t.category == Category.spam:
        status, reason = "closed", "spam: closed automatically"
    elif t.category != Category.support:
        status, route_to = "routed", route(t)
        reason = f"not a support request: routed to {route_to}"
    elif (hits := search(index, mail)) and (draft_out := engine.draft(mail, hits))[0]:
        status, (draft, sources) = "drafted", draft_out
    else:
        status, reason = "needs_human", "the documents don't answer this: write the reply, then add it to the docs"
    if t is not None and t.urgency == Urgency.high and status == "drafted":
        reason = "high urgency: a lead must approve this reply"

    cur = conn.execute(
        """insert into tickets (message_id, from_addr, from_name, subject, body, received_at, category, urgency, summary,
           injection, status, route, reason, draft, sources) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (mail["message_id"], mail["from"], mail["from_name"], mail["subject"], mail["body"], store.now(),
         t and t.category.value, t and t.urgency.value, t and t.summary, int(bool(t and t.suspected_injection)),
         status, route_to, reason, draft, json.dumps(sources)))
    tid = cur.lastrowid
    store.log(conn, tid, BOT, "received", {"from": mail["from"], "subject": mail["subject"]})
    if t is not None:
        store.log(conn, tid, engine.name, "triaged", {"category": t.category.value, "urgency": t.urgency.value,
                                                       "injection": t.suspected_injection})
    if draft:
        store.log(conn, tid, engine.name, "drafted", {"sources": len(sources)})
    if status == "needs_human" and t is not None and not t.suspected_injection and t.category == Category.support:
        conn.execute("insert into gaps (ticket_id, question, at) values (?, ?, ?)", (tid, mail["subject"], store.now()))
    conn.commit()
    return status


def process_inbox(imap, conn, engine, index: BM25, mailbox: str = "INBOX") -> dict:
    stats = {"emails": 0, "filed": 0, "duplicates": 0, "retry_later": 0}
    imap.select(mailbox)
    _, data = imap.uid("search", None, "UNSEEN")
    for uid in (data[0] or b"").split():
        stats["emails"] += 1
        _, parts = imap.uid("fetch", uid, "(BODY.PEEK[])")  # PEEK: reading doesn't mark it seen
        mail = parse(parts[0][1])
        try:
            status = handle(conn, engine, index, mail)
        except TransientError as e:
            conn.rollback()
            log.error("email %s will be retried: %s", mail["message_id"], e)
            stats["retry_later"] += 1
            continue  # leave it unseen
        stats["filed" if status else "duplicates"] += 1
        log.info("%s %s -> %s", mail["from"], mail["subject"], status or "already filed")
        imap.uid("store", uid, "+FLAGS", "(\\Seen)")
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    e = os.environ
    conn, engine = store.connect(), make_engine()
    index = BM25(load_folder(Path(e.get("KB_DIR", "kb"))))
    log.info("engine: %s, knowledge base: %d sections", engine.name, len(index.chunks))
    while True:
        try:
            ssl = e.get("IMAP_SSL", "true").lower() == "true"
            imap = (imaplib.IMAP4_SSL if ssl else imaplib.IMAP4)(e["IMAP_HOST"], int(e.get("IMAP_PORT", 993)))
            try:
                imap.login(e["IMAP_USER"], e["IMAP_PASSWORD"])
                stats = process_inbox(imap, conn, engine, index, e.get("IMAP_MAILBOX", "INBOX"))
                if stats["emails"]:
                    log.info("pass done: %s", stats)
            finally:
                imap.logout()
        except (imaplib.IMAP4.error, OSError) as err:
            log.error("mailbox unavailable, retrying: %s", err)
        time.sleep(int(e.get("POLL_SECONDS", 30)))


if __name__ == "__main__":
    main()
