"""The stock ledger: one source of truth for every channel.

- Every change is a movement (receipt, reserve, release, ship, adjust) with a reference. (kind, sku, ref) is unique,
  so a webhook delivered twice or an order polled twice changes stock once.
- An order reserves all of its units or none: a basket that can't be filled completely is marked oversold and raised
  as an exception, instead of half-reserving and confusing everyone.
- Bundles have no stock of their own: a starter set is available as often as its scarcest part, and selling one
  reserves the parts.
- What each channel is shown is computed, not stored: available minus the channel's safety buffer, capped.
"""
from collections import Counter

from .db import now, tx

PHYSICAL = {"receipt": (1, 0), "reserve": (0, 1), "release": (0, -1), "ship": (-1, -1), "adjust": (1, 0)}


class NotEnough(Exception):
    pass


def move(db, sku: str, kind: str, qty: int, ref: str, note: str = "") -> bool:
    """Apply one movement inside the caller's transaction. Returns False if this exact movement was already applied."""
    if db.execute("select 1 from movements where kind = ? and sku = ? and ref = ?", (kind, sku, ref)).fetchone():
        return False
    dh, dr = PHYSICAL[kind]
    db.execute("update skus set on_hand = on_hand + ?, reserved = reserved + ? where sku = ?", (dh * qty, dr * qty, sku))
    row = db.execute("select on_hand, reserved from skus where sku = ?", (sku,)).fetchone()
    db.execute("insert into movements (at, sku, kind, qty, on_hand_after, reserved_after, ref, note) values (?, ?, ?, ?, ?, ?, ?, ?)",
               (now(), sku, kind, qty, row["on_hand"], row["reserved"], ref, note[:300]))
    return True


def parts(db, sku: str) -> list[tuple[str, int]]:
    rows = db.execute("select part, qty from bundle_parts where bundle = ?", (sku,)).fetchall()
    return [(r["part"], r["qty"]) for r in rows] or [(sku, 1)]


def needs(db, lines: list[tuple[str, int]]) -> Counter:
    """Physical units per SKU for a basket (bundles expanded, the same SKU in two lines added up)."""
    out = Counter()
    for sku, qty in lines:
        if not db.execute("select 1 from skus where sku = ?", (sku,)).fetchone():
            raise KeyError(sku)
        for part, n in parts(db, sku):
            out[part] += n * qty
    return out


def available(db, sku: str) -> int:
    row = db.execute("select on_hand, reserved, is_bundle from skus where sku = ?", (sku,)).fetchone()
    if not row["is_bundle"]:
        return max(0, row["on_hand"] - row["reserved"])
    return min(available(db, p) // n for p, n in parts(db, sku))


def targets(db) -> dict[int, int]:
    """Quantity each listing should show right now. shortcut: recomputes every listing, fine to tens of thousands of listings."""
    out = {}
    for l in db.execute("select * from listings where enabled = 1"):
        q = available(db, l["sku"]) - l["buffer"]
        out[l["id"]] = max(0, min(q, l["max_qty"]) if l["max_qty"] is not None else q)
    return out


def exception(db, kind: str, detail: str, sku: str | None = None, order_id: int | None = None) -> None:
    if not db.execute("select 1 from exceptions where status = 'open' and kind = ? and coalesce(sku, '') = ? and coalesce(order_id, 0) = ?",
                      (kind, sku or "", order_id or 0)).fetchone():
        db.execute("insert into exceptions (at, kind, sku, order_id, detail) values (?, ?, ?, ?, ?)", (now(), kind, sku, order_id, detail))


def place_order(db, channel: str, external_id: str, lines: list[tuple[str, int]]) -> str:
    """Reserve a channel order. Idempotent: the same order again returns its current status and changes nothing."""
    ref = f"{channel}:{external_id}"
    with tx(db):
        old = db.execute("select status from orders where channel = ? and external_id = ?", (channel, external_id)).fetchone()
        if old:
            return old["status"]
        try:
            want = needs(db, lines)
        except KeyError as e:
            unknown = str(e).strip("'")
            want = None
        oid = db.execute("insert into orders (channel, external_id, status, created_at, updated_at) values (?, ?, 'reserved', ?, ?)",
                         (channel, external_id, now(), now())).lastrowid
        short = {} if want is None else {s: (n, available(db, s)) for s, n in want.items() if available(db, s) < n}
        if want is None or short:
            db.execute("update orders set status = 'oversold' where id = ?", (oid,))
            db.executemany("insert into order_lines (order_id, sku, qty, state) values (?, ?, ?, 'oversold')", [(oid, s, q) for s, q in lines])
            why = f"unknown SKU {unknown}" if want is None else "; ".join(f"{s}: needs {n}, {a} available" for s, (n, a) in short.items())
            exception(db, "oversold", f"{channel} order {external_id} can't be filled ({why})", next(iter(short), None), oid)
            return "oversold"
        for sku, n in want.items():
            move(db, sku, "reserve", n, ref, f"{channel} order {external_id}")
        db.executemany("insert into order_lines (order_id, sku, qty, state) values (?, ?, ?, 'reserved')", [(oid, s, q) for s, q in lines])
        return "reserved"


def retry_order(db, order_id: int, who: str) -> str:
    """An oversold order after new stock arrived: reserve it now, or say what's still missing."""
    with tx(db):
        o = db.execute("select * from orders where id = ?", (order_id,)).fetchone()
        if not o or o["status"] != "oversold":
            return o["status"] if o else "missing"
        lines = [(r["sku"], r["qty"]) for r in db.execute("select sku, qty from order_lines where order_id = ?", (order_id,))]
        want = needs(db, lines)
        if any(available(db, s) < n for s, n in want.items()):
            return "oversold"
        for sku, n in want.items():
            move(db, sku, "reserve", n, f"{o['channel']}:{o['external_id']}", f"reserved on retry by {who}")
        db.execute("update order_lines set state = 'reserved' where order_id = ?", (order_id,))
        db.execute("update orders set status = 'reserved', updated_at = ? where id = ?", (now(), order_id))
        db.execute("update exceptions set status = 'resolved', resolved_by = ?, resolved_at = ? where order_id = ? and status = 'open'", (who, now(), order_id))
        return "reserved"


def _finish(db, channel: str, external_id: str, kind: str, status: str) -> str:
    with tx(db):
        o = db.execute("select * from orders where channel = ? and external_id = ?", (channel, external_id)).fetchone()
        if not o:
            return "unknown"
        if o["status"] != "reserved":
            if o["status"] == "oversold" and status == "cancelled":  # nothing was reserved: just close it
                db.execute("update orders set status = 'cancelled', updated_at = ? where id = ?", (now(), o["id"]))
                db.execute("update exceptions set status = 'resolved', resolved_by = 'channel cancel', resolved_at = ? where order_id = ? and status = 'open'", (now(), o["id"]))
                return "cancelled"
            return o["status"]
        lines = [(r["sku"], r["qty"]) for r in db.execute("select sku, qty from order_lines where order_id = ?", (o["id"],))]
        for sku, n in needs(db, lines).items():
            move(db, sku, kind, n, f"{channel}:{external_id}", f"{channel} order {external_id} {status}")
        db.execute("update order_lines set state = ? where order_id = ?", ("shipped" if kind == "ship" else "released", o["id"]))
        db.execute("update orders set status = ?, updated_at = ? where id = ?", (status, now(), o["id"]))
        return status


def cancel_order(db, channel: str, external_id: str) -> str:
    return _finish(db, channel, external_id, "release", "cancelled")


def ship_order(db, channel: str, external_id: str) -> str:
    return _finish(db, channel, external_id, "ship", "shipped")


def receive(db, sku: str, qty: int, ref: str, note: str = "") -> bool:
    if db.execute("select is_bundle from skus where sku = ?", (sku,)).fetchone()[0]:
        raise ValueError("bundles have no stock of their own; receive the parts")
    with tx(db):
        return move(db, sku, "receipt", qty, ref, note)


def adjust(db, sku: str, delta: int, ref: str, note: str) -> bool:
    with tx(db):
        row = db.execute("select on_hand, reserved, is_bundle from skus where sku = ?", (sku,)).fetchone()
        if row["is_bundle"]:
            raise ValueError("bundles have no stock of their own; adjust the parts")
        if row["on_hand"] + delta < 0:
            raise ValueError(f"{sku} would go below zero")
        done = move(db, sku, "adjust", delta, ref, note)
        row = db.execute("select on_hand, reserved from skus where sku = ?", (sku,)).fetchone()
        if row["on_hand"] < row["reserved"]:  # e.g. units found damaged after they were sold
            exception(db, "short", f"{sku}: {row['reserved']} units promised to open orders, only {row['on_hand']} on hand", sku)
        return done
