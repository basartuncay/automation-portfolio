"""Crawl each competitor, keep a price history, and raise alerts that matter for the products we sell.

Guards against the classic scraper failure: when a site changes its layout, a crawl returns few or no products,
and a naive diff reports "everything removed". Here such a run is marked suspicious, nothing is changed, and one
"check the scraper" alert goes out instead of hundreds of false ones.
"""
import logging, os, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from alert_bot.core import Notifier
from price_monitor.core import crawl, diff

from . import db as store
from .report import send_digest

log = logging.getLogger("price-watch")


def money(v: float) -> str:
    return f"€{v:,.2f}"


def last_status(conn, competitor_id: int) -> str | None:
    r = conn.execute("select status from runs where competitor_id = ? and finished is not null order by id desc limit 1",
                     (competitor_id,)).fetchone()
    return r and r["status"]


def alert(conn, kind: str, message: str, competitor: str | None = None, pid: str | None = None, title: str | None = None):
    conn.execute("insert into alerts (at, kind, competitor, pid, title, message) values (?, ?, ?, ?, ?, ?)",
                 (store.now(), kind, competitor, pid, title, message))


def run_competitor(conn, comp, client: httpx.Client, drop_pct: float = 10.0) -> dict:
    name, started = comp["name"], store.now()
    run_id = conn.execute("insert into runs (competitor_id, started) values (?, ?)", (comp["id"], started)).lastrowid
    conn.commit()
    previous = last_status(conn, comp["id"])

    def finish(status, note=None, **counts):
        conn.execute("update runs set finished = ?, status = ?, note = ?, products = ?, changes = ?, errors = ? where id = ?",
                     (store.now(), status, note, counts.get("products"), counts.get("changes"), counts.get("errors"), run_id))
        conn.commit()
        return {"competitor": name, "status": status, "note": note, **counts}

    try:
        products, errors = crawl(comp["start_url"], max_pages=50, delay=float(os.environ.get("CRAWL_DELAY", 0.5)), client=client)
    except httpx.HTTPError as e:
        if previous != "failed":  # alert on the transition only, not on every retry
            alert(conn, "crawl_failed", f"{name}: the site could not be crawled ({type(e).__name__}). Will retry.", name)
        return finish("failed", f"{type(e).__name__}: {e}"[:300])
    if len(products) < comp["min_products"]:
        if previous != "suspicious":
            alert(conn, "scraper_check", f"{name}: only {len(products)} products found (expected at least "
                  f"{comp['min_products']}). The site layout probably changed: nothing was updated, check the scraper.", name)
        return finish("suspicious", f"{len(products)} products, expected ≥ {comp['min_products']}",
                      products=len(products), errors=len(errors))

    rows = conn.execute("select * from products where competitor_id = ?", (comp["id"],)).fetchall()
    first_run = not rows
    old = {r["pid"]: {"price": r["price"], "in_stock": bool(r["in_stock"]), "title": r["title"]} for r in rows if r["active"]}
    known = {r["pid"]: r for r in rows}
    changes = diff(old, products)
    ts = store.now()
    for p in products:
        prev = known.get(p.id)
        if prev is None:
            pid_row = conn.execute("""insert into products (competitor_id, pid, title, url, price, currency, in_stock,
                first_seen, last_seen) values (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (comp["id"], p.id, p.title, p.url, p.price, p.currency, int(p.in_stock), ts, ts)).lastrowid
            conn.execute("insert into prices (product_id, at, price, in_stock) values (?, ?, ?, ?)", (pid_row, ts, p.price, int(p.in_stock)))
            continue
        if prev["price"] != p.price or bool(prev["in_stock"]) != p.in_stock or not prev["active"]:
            conn.execute("insert into prices (product_id, at, price, in_stock) values (?, ?, ?, ?)", (prev["id"], ts, p.price, int(p.in_stock)))
        conn.execute("update products set title = ?, url = ?, price = ?, currency = ?, in_stock = ?, active = 1, last_seen = ? where id = ?",
                     (p.title, p.url, p.price, p.currency, int(p.in_stock), ts, prev["id"]))
    removed = [c["id"] for c in changes if c["change"] == "removed"]
    conn.executemany("update products set active = 0 where competitor_id = ? and pid = ?", [(comp["id"], pid) for pid in removed])

    if not first_run:  # the first crawl is a baseline: everything is "new", nothing is news
        ours = {r["pid"]: r for r in conn.execute("select * from our_products")}
        for c in changes:
            mine = ours.get(c["id"])
            if not mine:
                continue  # we only alert on products we sell; everything is still recorded
            t, our = c["title"], mine["our_price"]
            if c["change"] == "price":
                if c["new"] < our <= c["old"]:
                    alert(conn, "undercut", f"{name} undercuts you on {t}: {money(c['new'])} vs your {money(our)} "
                          f"(was {money(c['old'])}).", name, c["id"], t)
                elif c["new"] <= c["old"] * (1 - drop_pct / 100):
                    alert(conn, "price_drop", f"{name} cut {t} by {100 * (1 - c['new'] / c['old']):.0f}%: "
                          f"{money(c['old'])} → {money(c['new'])} (yours {money(our)}).", name, c["id"], t)
            elif c["change"] == "new" and c["new"] < our:
                alert(conn, "undercut", f"{name} now lists {t} at {money(c['new'])}, below your {money(our)}.", name, c["id"], t)
            elif c["change"] == "stock":
                state = "back in stock" if c["new"] else "out of stock"
                alert(conn, "stock", f"{name}: {t} is {state}.", name, c["id"], t)
            elif c["change"] == "removed":
                alert(conn, "stock", f"{name} no longer lists {t}.", name, c["id"], t)
    note = "baseline: first crawl, no alerts" if first_run else None
    return finish("ok", note, products=len(products), changes=0 if first_run else len(changes), errors=len(errors))


def notify_pending(conn, notifier: Notifier | None) -> int:
    """Send unsent alerts in order; an alert is marked sent only after the channel accepted it."""
    if notifier is None or not (notifier.slack or (notifier.tg_token and notifier.tg_chat)):
        return 0
    sent = 0
    for a in conn.execute("select * from alerts where notified = 0 order by id").fetchall():
        try:
            notifier.send(a["message"])
        except (RuntimeError, httpx.HTTPError) as e:
            log.error("notification failed, will retry next pass: %s", e)
            break
        conn.execute("update alerts set notified = 1 where id = ?", (a["id"],))
        conn.commit()
        sent += 1
    return sent


def run_all(conn, client: httpx.Client, notifier: Notifier | None) -> list[dict]:
    results = [run_competitor(conn, c, client, float(os.environ.get("DROP_PCT", 10)))
               for c in conn.execute("select * from competitors order by id").fetchall()]
    conn.commit()
    notify_pending(conn, notifier)
    return results


def due(conn, key: str, every: timedelta) -> bool:
    r = conn.execute("select value from meta where key = ?", (key,)).fetchone()
    return r is None or datetime.now(timezone.utc) - datetime.fromisoformat(r["value"]) >= every


def stamp(conn, key: str) -> None:
    conn.execute("insert into meta (key, value) values (?, ?) on conflict(key) do update set value = excluded.value",
                 (key, datetime.now(timezone.utc).isoformat()))
    conn.commit()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    e = os.environ
    conn = store.connect()
    if conn.execute("select 1 from meta where key = 'last_digest'").fetchone() is None:
        stamp(conn, "last_digest")  # first scheduled digest goes out one period after the first start
    store.load_config(conn, Path(e.get("COMPETITORS", "competitors.json")), Path(e.get("CATALOG", "our_catalog.csv")))
    notifier_client = httpx.Client(timeout=20)
    notifier = Notifier(notifier_client, slack_webhook=e.get("SLACK_WEBHOOK_URL") or None,
                        telegram_token=e.get("TELEGRAM_BOT_TOKEN") or None, telegram_chat=e.get("TELEGRAM_CHAT_ID") or None)
    crawl_every = timedelta(minutes=float(e.get("CRAWL_EVERY_MINUTES", 360)))
    digest_every = timedelta(days=float(e.get("DIGEST_EVERY_DAYS", 7)))
    with httpx.Client(headers={"User-Agent": e.get("USER_AGENT", "price-watch/1.0")}, timeout=30, follow_redirects=True) as client:
        while True:
            asked = conn.execute("select * from requests where done = 0 order by id").fetchall()
            kinds = {r["kind"] for r in asked}
            if "crawl" in kinds or due(conn, "last_crawl", crawl_every):
                stamp(conn, "last_crawl")
                for r in run_all(conn, client, notifier):
                    log.info("%s", r)
            if "digest" in kinds or (e.get("DIGEST_TO") and due(conn, "last_digest", digest_every)):
                if e.get("DIGEST_TO"):
                    send_digest(conn, e["DIGEST_TO"])
                    stamp(conn, "last_digest")
                    log.info("digest sent to %s", e["DIGEST_TO"])
            if asked:
                conn.executemany("update requests set done = 1 where id = ?", [(r["id"],) for r in asked])
                conn.commit()
            notify_pending(conn, notifier)  # retries alerts a channel outage held back
            time.sleep(2)


if __name__ == "__main__":
    main()
