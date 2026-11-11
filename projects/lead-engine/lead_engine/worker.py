"""The pipeline loop: enrich and score new leads, enroll the qualified ones, send what's due inside the window,
read replies, and push interested leads to the CRM. Each stage fails on its own: an IMAP hiccup doesn't stop sending."""
import logging, os, time

from . import db as store
from .enrich import DataAPI, process
from .outreach import Mailer, enroll_qualified, poll_inbox, push_interested, send_due

log = logging.getLogger("worker")


def cycle(conn, api: DataAPI, mailer: Mailer) -> dict:
    out = {}
    for name, step in (("enrich", lambda: process(conn, api)), ("enroll", lambda: enroll_qualified(conn)),
                       ("send", lambda: send_due(conn, mailer)), ("replies", lambda: poll_inbox(conn)), ("crm", lambda: push_interested(conn))):
        try:
            out[name] = step()
        except Exception as e:  # noqa: BLE001 - one stage failing must not stop the others
            log.exception("%s failed", name)
            out[name] = f"error: {e}"
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    api, mailer, every = DataAPI(), Mailer(), float(os.environ.get("POLL_SECONDS", 30))
    while True:
        conn = store.connect()
        try:
            out = cycle(conn, api, mailer)
            enriched = out["enrich"] if isinstance(out["enrich"], dict) else {}
            if enriched.get("scored") or enriched.get("disqualified") or any(out[k] for k in ("enroll", "send", "replies", "crm")):
                log.info("%s", out)
            wait = enriched.get("wait", 0)
        finally:
            conn.close()
        time.sleep(max(every, wait))


if __name__ == "__main__":
    main()
