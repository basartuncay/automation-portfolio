"""Background loop: push quantities every second (only what changed), poll Amazon orders, reconcile periodically."""
import logging, os, time

from . import db as store
from .channels import Amazon, Shopify, Temporary, Throttled, Warehouse
from .sync import poll_amazon, push, reconcile

log = logging.getLogger("worker")


def clients() -> dict:
    return {"shopify": Shopify(), "amazon": Amazon()}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    every, amazon_every = float(os.environ.get("PUSH_SECONDS", 1)), float(os.environ.get("AMAZON_POLL_SECONDS", 60))
    recon_every = float(os.environ.get("RECONCILE_MINUTES", 60)) * 60
    c, wms = clients(), Warehouse()
    last_poll = last_recon = 0.0
    paused: dict[str, float] = {}
    conn = store.connect()
    store.seed_catalog(conn)
    while True:
        try:
            active = {k: v for k, v in c.items() if paused.get(k, 0) <= time.time()}
            r = push(conn, active)
            for ch, wait in r["waits"].items():
                paused[ch] = time.time() + wait
            if r["pushed"]:
                log.info("pushed %s quantities", r["pushed"])
            if time.time() - last_poll >= amazon_every and paused.get("amazon", 0) <= time.time():
                last_poll = time.time()
                try:
                    if n := poll_amazon(conn, c["amazon"]):
                        log.info("%s new Amazon orders", n)
                except Throttled as e:
                    paused["amazon"] = time.time() + e.wait
                except Temporary as e:
                    log.warning("amazon poll: %s", e)
            if recon_every and time.time() - last_recon >= recon_every:
                last_recon = time.time()
                rep = reconcile(conn, wms, c)
                log.info("reconciliation #%s: %s stock differences, %s channel drifts", rep["id"], sum(1 for s in rep["stock"] if s["diff"]), len(rep["drift"]))
        except Exception:
            log.exception("cycle failed")
        time.sleep(every)


if __name__ == "__main__":
    main()
