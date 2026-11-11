"""Background loop: pick up HR changes a webhook may have missed, re-plan everyone (start and end dates arrive with
the calendar, not with a webhook), run due actions, and scan for drift."""
import logging, os, time

from . import db as store
from .engine import plan_all, run, scan
from .hris import HRIS, poll
from .targets import GitHub, Google, Slack

log = logging.getLogger("worker")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    clients, hr = {"google": Google(), "slack": Slack(), "github": GitHub()}, HRIS()
    every, poll_every = float(os.environ.get("RUN_SECONDS", 2)), float(os.environ.get("HRIS_POLL_SECONDS", 300))
    drift_every = float(os.environ.get("DRIFT_MINUTES", 60)) * 60
    last_poll = last_drift = 0.0
    while True:
        conn = store.connect()
        try:
            if time.time() - last_poll >= poll_every:
                poll(conn, hr)
                last_poll = time.time()
            plan_all(conn)
            r = run(conn, clients)
            if r["done"] or r["failed"]:
                log.info("actions: %s", r)
            if drift_every and time.time() - last_drift >= drift_every:
                found = scan(conn, clients)
                last_drift = time.time()
                if found:
                    log.warning("drift: %s", found)
        except Exception:
            log.exception("cycle failed")
        finally:
            conn.close()
        time.sleep(every)


if __name__ == "__main__":
    main()
