"""Background loop: report usage to Stripe every few seconds and restrict accounts whose grace period is over."""
import logging, os, time

from . import db as store
from .billing import enforce_grace, report
from .stripe_api import Stripe, StripeError

log = logging.getLogger("worker")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stripe, every, grace = Stripe(), float(os.environ.get("REPORT_SECONDS", 10)), float(os.environ.get("GRACE_DAYS", 7))
    while True:
        conn = store.connect()
        try:
            if n := report(conn, stripe):
                log.info("reported %s usage events to Stripe", n)
            if out := enforce_grace(conn, grace):
                log.info("restricted after the grace period: %s", out)
        except StripeError as e:
            log.warning("Stripe: %s (will retry)", e)
        except Exception:
            log.exception("cycle failed")
        finally:
            conn.close()
        time.sleep(every)


if __name__ == "__main__":
    main()
