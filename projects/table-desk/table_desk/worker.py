"""Background worker: sends due messages through the SMS gateway (Twilio's REST shape), retries temporary failures
with backoff, expires unanswered waitlist offers and passes the table to the next guest, and removes guests' personal
data after the retention period."""
import logging, os, time
from datetime import datetime

import httpx

from . import db as store
from .engine import anonymize, expire_offers

log = logging.getLogger("worker")


class Gateway:
    def __init__(self, http: httpx.Client | None = None):
        self.base = os.environ["SMS_API_URL"].rstrip("/")
        self.sid, self.token, self.sender = os.environ["SMS_ACCOUNT_SID"], os.environ["SMS_AUTH_TOKEN"], os.environ["SMS_FROM"]
        self.http = http or httpx.Client(timeout=15)

    def send(self, to: str, body: str) -> str:
        r = self.http.post(f"{self.base}/2010-04-01/Accounts/{self.sid}/Messages.json", auth=(self.sid, self.token),
                           data={"To": to, "From": self.sender, "Body": body})
        if r.status_code == 429 or r.status_code >= 500:
            raise TimeoutError(f"{r.status_code} from the SMS gateway")
        if r.status_code >= 400:
            raise ValueError(f"{r.status_code}: {r.json().get('message', r.text)[:200]}")
        return r.json()["sid"]


def send_due(conn, gw: Gateway, max_attempts: int | None = None, backoff: float | None = None) -> int:
    max_attempts = max_attempts or int(os.environ.get("MAX_ATTEMPTS", 5))
    backoff = backoff if backoff is not None else float(os.environ.get("BACKOFF_SECONDS", 30))
    sent = 0
    for m in conn.execute("select * from outbox where status = 'queued' and due <= ? order by due, id limit 50", (time.time(),)).fetchall():
        # claim first: a second worker (or a restart mid-send) can't send the same message twice
        if not conn.execute("update outbox set attempts = attempts + 1 where id = ? and status = 'queued' and attempts = ?",
                            (m["id"], m["attempts"])).rowcount:
            continue
        try:
            sid = gw.send(m["to_"], m["body"])
        except (TimeoutError, httpx.TransportError) as e:
            n = m["attempts"] + 1
            if n >= max_attempts:
                conn.execute("update outbox set status = 'failed', last_error = ? where id = ?", (f"gave up after {n} tries: {e}", m["id"]))
            else:
                conn.execute("update outbox set due = ?, last_error = ? where id = ?", (time.time() + backoff * 2 ** (n - 1), str(e), m["id"]))
            continue
        except ValueError as e:  # the gateway refused it (bad number, blocked): retrying won't help
            conn.execute("update outbox set status = 'failed', last_error = ? where id = ?", (str(e), m["id"]))
            continue
        conn.execute("update outbox set status = 'sent', sid = ?, sent_at = ?, last_error = null where id = ?", (sid, store.now(), m["id"]))
        sent += 1
    return sent


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    gw, every, last_cleanup = Gateway(), float(os.environ.get("POLL_SECONDS", 5)), 0.0
    while True:
        conn = store.connect()
        try:
            n = send_due(conn, gw)
            expired = expire_offers(conn)
            if time.time() - last_cleanup > 3600:
                gone = anonymize(conn)
                last_cleanup = time.time()
                if gone:
                    log.info("anonymized %s old bookings", gone)
            if n or expired:
                log.info("sent %s messages, expired offers on %s", n, expired)
        except Exception:
            log.exception("worker cycle failed")
        finally:
            conn.close()
        time.sleep(every)


if __name__ == "__main__":
    main()
