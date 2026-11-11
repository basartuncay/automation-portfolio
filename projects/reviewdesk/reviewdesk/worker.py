"""Background loop: fetch reviews every SYNC_MINUTES, post scheduled replies when their review window is over."""
import logging, os, time

from . import db as store
from .desk import Google, post_due, sync

log = logging.getLogger("worker")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    google, every, sync_every, last = Google(), float(os.environ.get("POLL_SECONDS", 30)), float(os.environ.get("SYNC_MINUTES", 15)) * 60, 0.0
    while True:
        conn = store.connect()
        try:
            if time.time() - last >= sync_every:
                r = sync(conn, google)
                last = time.time()
                if r["new"] or r["changed"]:
                    log.info("sync: %s", r)
            if n := post_due(conn, google):
                log.info("posted %s scheduled replies", n)
        except Exception:
            log.exception("cycle failed")
        finally:
            conn.close()
        time.sleep(every)


if __name__ == "__main__":
    main()
