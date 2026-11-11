"""Background loop: create labels for ready orders (if AUTO_LABEL), mark printed parcels as fulfilled in the shop,
and flag shipped parcels the carrier hasn't scanned for days."""
import logging, os, time
from pathlib import Path

from . import db as store
from .flow import Carrier, Shop, fulfil, label, stuck
from .web import CARRIERS

log = logging.getLogger("worker")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    carrier, shop = Carrier(), Shop()
    every, auto = float(os.environ.get("POLL_SECONDS", 5)), os.environ.get("AUTO_LABEL", "true").lower() == "true"
    stuck_days, labels = float(os.environ.get("STUCK_DAYS", 3)), Path(os.environ.get("DATA_DIR", "data")) / "labels"
    while True:
        conn = store.connect()
        try:
            if auto:
                for (oid,) in conn.execute("select id from orders where status = 'ready' order by received_at").fetchall():
                    log.info("order %s: %s", oid, label(conn, carrier, oid, labels))
            if n := fulfil(conn, shop, CARRIERS):
                log.info("fulfilled %s parcels in the shop", n)
            if out := stuck(conn, stuck_days):
                log.warning("no scan for %s days: %s", stuck_days, out)
        except Exception:
            log.exception("cycle failed")
        finally:
            conn.close()
        time.sleep(every)


if __name__ == "__main__":
    main()
