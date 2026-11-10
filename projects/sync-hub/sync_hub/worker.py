"""Queue worker and CRM poller.

- Events are processed oldest first, but only one at a time per record: a later change to the same customer waits
  behind an earlier one that is retrying, so changes never apply out of order. Other customers keep flowing.
- Retryable failures back off exponentially (or as long as Retry-After says); after MAX_ATTEMPTS the event is
  parked as a dead letter with its error. Nothing is dropped: a person can replay it from the panel.
- The CRM has no webhooks here, so it is polled with an updatedAfter cursor that only moves forward after the
  events are safely queued.
"""
import json, logging, os, time

import httpx

from . import db as store
from .clients import CRM, Permanent, Retryable, Shop
from .engine import process

log = logging.getLogger("sync-hub")
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", 6))
BACKOFF = float(os.environ.get("BACKOFF_SECONDS", 2))


def enqueue(conn, source: str, external_id: str, entity: str, topic: str, payload: dict) -> bool:
    """Store an incoming change once. Returns False for a duplicate delivery (same webhook or poll result again)."""
    cur = conn.execute("""insert into events (source, external_id, entity, topic, payload, received_at, next_at)
                          values (?, ?, ?, ?, ?, ?, 0) on conflict(external_id) do nothing""",
                       (source, external_id, entity, topic, json.dumps(payload), store.now()))
    conn.commit()
    return cur.rowcount == 1


def due_events(conn, limit: int = 20):
    return conn.execute("""select * from events e where status = 'queued' and next_at <= ?
                           and not exists (select 1 from events p where p.entity = e.entity and p.status = 'queued' and p.id < e.id)
                           order by id limit ?""", (time.time(), limit)).fetchall()


def run_event(conn, crm, shop, ev) -> str:
    try:
        result = process(conn, crm, shop, ev)
        conn.execute("update events set status = 'done', done_at = ?, attempts = attempts + 1, last_error = null where id = ?",
                     (store.now(), ev["id"]))
        conn.commit()
        return result
    except Retryable as e:
        conn.rollback()
        attempts = ev["attempts"] + 1
        if attempts >= MAX_ATTEMPTS:
            conn.execute("update events set status = 'dead', attempts = ?, last_error = ? where id = ?", (attempts, str(e), ev["id"]))
            outcome = "dead"
        else:
            wait = max(e.retry_after or 0, BACKOFF * 2 ** (attempts - 1))
            conn.execute("update events set attempts = ?, next_at = ?, last_error = ? where id = ?",
                         (attempts, time.time() + wait, str(e), ev["id"]))
            outcome = "retry"
        conn.commit()
        return outcome
    except Permanent as e:
        conn.rollback()
        conn.execute("update events set status = 'dead', attempts = attempts + 1, last_error = ? where id = ?", (str(e), ev["id"]))
        conn.commit()
        return "dead"


def drain(conn, crm, shop) -> dict:
    stats = {}
    for ev in due_events(conn):
        r = run_event(conn, crm, shop, ev)
        stats[r] = stats.get(r, 0) + 1
    return stats


def poll_crm(conn, crm) -> int:
    if not crm.connected():
        return 0
    row = conn.execute("select cursor from connections where name = 'crm'").fetchone()
    cursor = row["cursor"] or ""
    n = 0
    for c in crm.list_updated(cursor):
        n += enqueue(conn, "crm", f"crm:{c['id']}:{c['updatedAt']}", f"crm:{c['id']}", "contact.updated", c)
        cursor = max(cursor, c["updatedAt"])
    conn.execute("update connections set cursor = ? where name = 'crm'", (cursor,))
    conn.commit()
    return n


def backfill(conn, crm, shop) -> int:
    """Queue every record on both sides once (first connection, or after fixing a mapping)."""
    stamp = str(time.time())
    n = sum(enqueue(conn, "shop", f"shop:backfill:{c['id']}:{stamp}", f"shop:{c['id']}", "backfill", c) for c in shop.all())
    if crm.connected():
        n += sum(enqueue(conn, "crm", f"crm:backfill:{c['id']}:{stamp}", f"crm:{c['id']}", "backfill", c) for c in crm.list_updated(""))
    return n


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    conn = store.connect()
    poll_every = float(os.environ.get("POLL_SECONDS", 30))
    last_poll = 0.0
    with httpx.Client(timeout=20) as http:
        crm, shop = CRM(conn, http), Shop(http)
        while True:
            if time.time() - last_poll >= poll_every:
                try:
                    if (n := poll_crm(conn, crm)):
                        log.info("polled %d CRM change(s)", n)
                except (Retryable, Permanent) as e:
                    log.warning("CRM poll failed, will retry: %s", e)
                last_poll = time.time()
            if (stats := drain(conn, crm, shop)):
                log.info("processed %s", stats)
            time.sleep(0.5)


if __name__ == "__main__":
    main()
