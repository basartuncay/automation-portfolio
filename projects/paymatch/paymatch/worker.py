"""Background loop: mirror open invoices, match new payments, book matches in the accounting system, send dunning
letters for what's still overdue. Each stage fails on its own."""
import logging, os, time

from . import db as store
from .accounting import Accounting, dunning, push, send_mail, sync
from .match import run

log = logging.getLogger("worker")


def dunning_due(conn) -> bool:
    """Reminders go out once a day at DUNNING_AT (local time), after that morning's statement had a chance to arrive."""
    at = os.environ.get("DUNNING_AT", "09:00")
    if at == "off":
        return False
    today = time.strftime("%Y-%m-%d")
    if time.strftime("%H:%M") < at or conn.execute("select 1 from settings where key = 'dunning_ran' and value = ?", (today,)).fetchone():
        return False
    conn.execute("insert into settings values ('dunning_ran', ?) on conflict(key) do update set value = excluded.value", (today,))
    return True


def cycle(conn, api: Accounting) -> dict:
    out = {}
    for name, step in (("sync", lambda: sync(conn, api)), ("match", lambda: run(conn)), ("book", lambda: push(conn, api)),
                       ("dunning", lambda: dunning(conn) if dunning_due(conn) else []), ("mail", lambda: send_mail(conn))):
        try:
            out[name] = step()
        except Exception as e:  # noqa: BLE001 - e.g. the accounting API is down: matching and mail still run
            log.warning("%s failed: %s", name, e)
            out[name] = f"error: {e}"
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    api, every = Accounting(), float(os.environ.get("POLL_SECONDS", 60))
    while True:
        conn = store.connect()
        try:
            out = cycle(conn, api)
            if out.get("book") or out.get("dunning") or (isinstance(out.get("match"), dict) and any(out["match"].values())):
                log.info("%s", out)
        finally:
            conn.close()
        time.sleep(every)


if __name__ == "__main__":
    main()
