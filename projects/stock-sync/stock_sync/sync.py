"""Keeping channels right: push the computed quantity to every listing that shows something else, poll Amazon for
orders (Shopify and the warehouse push webhooks), and reconcile against the warehouse count and the channels."""
import json, logging
from datetime import datetime, timedelta, timezone

import httpx

from .channels import Temporary, Throttled
from .db import now, tx
from .ledger import adjust, cancel_order, exception, place_order, ship_order, targets

log = logging.getLogger("sync")


def note(db, channel: str, sku: str | None, action: str, detail: str = "") -> None:
    db.execute("insert into sync_log (at, channel, sku, action, detail) values (?, ?, ?, ?, ?)", (now(), channel, sku, action, detail[:500]))


def push(db, clients: dict) -> dict:
    """Push every listing whose shown quantity differs from what it should be. Zero first: selling out is the urgent case."""
    want = targets(db)
    rows = db.execute("select * from listings where enabled = 1").fetchall()
    todo = sorted((l for l in rows if want[l["id"]] != l["last_pushed"]), key=lambda l: (want[l["id"]] != 0, l["id"]))
    done, waits, stopped = 0, {}, set()
    for l in todo:
        if l["channel"] in stopped or l["channel"] not in clients:
            continue
        qty = want[l["id"]]
        try:
            clients[l["channel"]].set_quantity(l["channel_ref"], qty)
        except Throttled as e:
            stopped.add(l["channel"])
            waits[l["channel"]] = e.wait
            note(db, l["channel"], l["sku"], "throttled", str(e))
            continue
        except (Temporary, httpx.TransportError) as e:
            stopped.add(l["channel"])
            note(db, l["channel"], l["sku"], "retry", str(e))
            continue
        except (httpx.HTTPStatusError, ValueError) as e:
            msg = e.response.text[:200] if isinstance(e, httpx.HTTPStatusError) else str(e)
            db.execute("update listings set push_error = ? where id = ?", (msg, l["id"]))
            with tx(db):
                exception(db, "push_failed", f"{l['channel']} refused the quantity for {l['sku']}: {msg}", l["sku"])
            continue
        db.execute("update listings set last_pushed = ?, pushed_at = ?, push_error = null where id = ?", (qty, now(), l["id"]))
        db.execute("update exceptions set status = 'resolved', resolved_by = 'next push', resolved_at = ? where kind = 'push_failed' and sku = ? and status = 'open'", (now(), l["sku"]))
        note(db, l["channel"], l["sku"], "pushed", f"{'—' if l['last_pushed'] is None else l['last_pushed']} → {qty}")
        done += 1
    return {"pushed": done, "waits": waits}


def poll_amazon(db, amazon) -> int:
    """Amazon has no order webhooks for this: poll with an overlap, the ledger ignores what it has already seen."""
    row = db.execute("select value from cursors where name = 'amazon_orders'").fetchone()
    since = row["value"] if row else (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    overlap = (datetime.strptime(since, "%Y-%m-%dT%H:%M:%SZ") - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    refs = {r["channel_ref"]: r["sku"] for r in db.execute("select channel_ref, sku from listings where channel = 'amazon'")}
    seen = 0
    for o in amazon.orders_since(overlap):
        lines = [(refs.get(i["SellerSKU"], i["SellerSKU"]), int(i["QuantityOrdered"])) for i in o["items"]]
        status = o["OrderStatus"]
        if status in ("Pending", "Unshipped", "PartiallyShipped", "Shipped"):
            before = db.execute("select status from orders where channel = 'amazon' and external_id = ?", (o["AmazonOrderId"],)).fetchone()
            result = place_order(db, "amazon", o["AmazonOrderId"], lines)
            if not before:
                note(db, "amazon", None, "order", f"{o['AmazonOrderId']}: {result}")
                seen += 1
        if status == "Canceled" and cancel_order(db, "amazon", o["AmazonOrderId"]) == "cancelled":
            note(db, "amazon", None, "cancelled", o["AmazonOrderId"])
        since = max(since, o["LastUpdateDate"])
    db.execute("insert into cursors values ('amazon_orders', ?) on conflict(name) do update set value = excluded.value", (since,))
    return seen


def reconcile(db, wms, clients: dict) -> dict:
    """Compare the ledger with the warehouse's physical count and each channel with what we pushed.

    A count difference is only booked when the same difference shows in two runs in a row: a shipment still on its
    way as a webhook would otherwise look like missing stock. Channel quantities that differ from what we pushed
    (someone edited them by hand) are re-pushed."""
    count = wms.snapshot()
    pending = json.loads((db.execute("select value from cursors where name = 'recon_pending'").fetchone() or ['{}'])[0])
    rows, new_pending = [], {}
    rid = db.execute("insert into recon_runs (at, report) values (?, '{}')", (now(),)).lastrowid
    for s in db.execute("select sku, on_hand from skus where is_bundle = 0 order by sku").fetchall():
        theirs = count.get(s["sku"], 0)
        diff = theirs - s["on_hand"]
        action = "ok"
        if diff and pending.get(s["sku"]) == diff:
            try:
                adjust(db, s["sku"], diff, f"recon:{rid}", f"warehouse count {theirs}, ledger {s['on_hand']} (seen twice)")
                action = "adjusted"
            except ValueError as e:
                action = f"not adjusted: {e}"
        elif diff:
            new_pending[s["sku"]] = diff
            action = "watching"
        rows.append({"sku": s["sku"], "ledger": s["on_hand"], "warehouse": theirs, "diff": diff, "action": action})
    drift = []
    for name, client in clients.items():
        listings = db.execute("select * from listings where channel = ? and enabled = 1 and last_pushed is not null", (name,)).fetchall()
        try:
            shown = client.quantities([l["channel_ref"] for l in listings])
        except (Throttled, Temporary, httpx.HTTPError) as e:
            drift.append({"channel": name, "error": str(e)})
            continue
        for l in listings:
            if shown.get(l["channel_ref"]) != l["last_pushed"]:
                drift.append({"channel": name, "sku": l["sku"], "pushed": l["last_pushed"], "shown": shown.get(l["channel_ref"])})
                db.execute("update listings set last_pushed = null where id = ?", (l["id"],))  # forces a re-push
                note(db, name, l["sku"], "drift", f"showed {shown.get(l['channel_ref'])}, we had pushed {l['last_pushed']}; re-pushing")
    db.execute("insert into cursors values ('recon_pending', ?) on conflict(name) do update set value = excluded.value", (json.dumps(new_pending),))
    report = {"stock": rows, "drift": drift}
    db.execute("update recon_runs set report = ? where id = ?", (json.dumps(report), rid))
    return {"id": rid, **report}


def handle_shopify(db, topic: str, order: dict) -> str:
    lines = [(li["sku"], int(li["quantity"])) for li in order.get("line_items", [])]
    oid = str(order["id"])
    if topic == "orders/create":
        r = place_order(db, "shopify", oid, lines)
    elif topic == "orders/cancelled":
        r = cancel_order(db, "shopify", oid)
    else:
        return "ignored"
    note(db, "shopify", None, topic.split("/")[1], f"{order.get('name', oid)}: {r}")
    return r


def handle_wms(db, event: dict) -> str:
    """Warehouse events: goods received, an order shipped, a stock count correction."""
    from .ledger import receive
    if event["type"] == "receipt":
        ok = receive(db, event["sku"], int(event["qty"]), f"wms:{event['receipt_id']}", f"received {event['qty']}")
        note(db, "warehouse", event["sku"], "receipt", f"+{event['qty']}" + ("" if ok else " (already booked)"))
        return "booked" if ok else "duplicate"
    if event["type"] == "shipment":
        channel, _, ext = event["order_ref"].partition(":")
        r = ship_order(db, channel, ext)
        note(db, "warehouse", None, "shipped", f"{event['order_ref']}: {r}")
        return r
    return "ignored"
