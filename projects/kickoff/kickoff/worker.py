"""Background loop: send due emails (with the signed PDF attached where there is one), expire nothing silently,
and run provisioning steps for paid clients."""
import logging, os, smtplib, time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

from . import db as store
from .services import Projects, Storage

log = logging.getLogger("worker")


def send_due(conn) -> int:
    e = os.environ
    sent = 0
    for m in conn.execute("select * from outbox where status = 'queued' and due <= ? order by due, id limit 30", (time.time(),)).fetchall():
        if not conn.execute("update outbox set attempts = attempts + 1, due = ? where id = ? and status = 'queued' and attempts = ?",
                            (time.time() + 60, m["id"], m["attempts"])).rowcount:
            continue  # claimed by another worker
        msg = EmailMessage()
        msg["From"] = formataddr((e.get("STUDIO_NAME", "Basar Tuncay"), e["FROM_EMAIL"]))
        msg["To"], msg["Subject"], msg["Message-ID"] = m["to_"], m["subject"], make_msgid(domain=e["FROM_EMAIL"].split("@")[1])
        msg.set_content(m["body"])
        if m["attachment"] and Path(m["attachment"]).exists():
            msg.add_attachment(Path(m["attachment"]).read_bytes(), maintype="application", subtype="pdf", filename="agreement-signed.pdf")
        try:
            with smtplib.SMTP(e["SMTP_HOST"], int(e.get("SMTP_PORT", 587)), timeout=20) as s:
                if e.get("SMTP_STARTTLS", "true").lower() == "true":
                    s.starttls()
                if e.get("SMTP_USER"):
                    s.login(e["SMTP_USER"], e["SMTP_PASSWORD"])
                s.send_message(msg)
        except (smtplib.SMTPException, OSError) as err:
            n = m["attempts"] + 1
            conn.execute("update outbox set status = ?, error = ?, due = ? where id = ?",
                         ("failed" if n >= 5 else "queued", str(err)[:300], time.time() + 30 * 2 ** n, m["id"]))
            continue
        conn.execute("update outbox set status = 'sent', sent_at = ?, error = null where id = ?", (store.now(), m["id"]))
        if m["client_id"]:
            store.event(conn, m["client_id"], "email", f"{m['kind'].replace('_', ' ')}: {m['subject']}")
        sent += 1
    return sent


def main() -> None:
    from .flow import provision
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    projects, storage, every = Projects(), Storage(), float(os.environ.get("POLL_SECONDS", 5))
    while True:
        conn = store.connect()
        try:
            for name, step in (("email", lambda: send_due(conn)), ("provision", lambda: provision(conn, projects, storage))):
                try:
                    if n := step():
                        log.info("%s: %s", name, n)
                except Exception:
                    log.exception("%s failed", name)
        finally:
            conn.close()
        time.sleep(every)


if __name__ == "__main__":
    main()
