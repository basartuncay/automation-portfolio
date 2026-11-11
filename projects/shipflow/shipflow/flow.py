"""The path of an order: received → checked (rules, address, customs) → label → printed in the day's batch →
fulfilled in the shop with tracking → tracked until delivered, with problems surfaced.

Labels are created at most once per order: the carrier is asked for an existing parcel with the order number before
a new one is made, so a retry after a timeout never pays for two labels."""
import json, os, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from pypdf import PdfWriter

from .db import audit, now
from .rules import EU, decide

CARRIER_IDS = {"dhl": 8, "dhl_express": 1103, "dpd": 5}  # the carrier's shipping-method ids
STUCK_DAYS = 3


class Carrier:
    """Sendcloud-style multi-carrier API: one endpoint for DHL, DPD and DHL Express parcels."""

    def __init__(self, http=None):
        self.base = os.environ["CARRIER_URL"].rstrip("/")
        self.auth = (os.environ["CARRIER_KEY"], os.environ["CARRIER_SECRET"])
        self.http = http or httpx.Client(timeout=30)

    def find(self, order_number: str) -> dict | None:
        r = self.http.get(f"{self.base}/api/v2/parcels", params={"order_number": order_number}, auth=self.auth)
        r.raise_for_status()
        return (r.json()["parcels"] or [None])[0]

    def create(self, payload: dict) -> httpx.Response:
        return self.http.post(f"{self.base}/api/v2/parcels", json={"parcel": payload}, auth=self.auth)

    def label_pdf(self, url: str) -> bytes:
        r = self.http.get(url, auth=self.auth)
        r.raise_for_status()
        return r.content


class Shop:
    def __init__(self, http=None):
        self.base = os.environ["SHOP_URL"].rstrip("/") + "/admin/api/2026-10"
        self.h = {"X-Shopify-Access-Token": os.environ["SHOP_TOKEN"]}
        self.http = http or httpx.Client(timeout=20)

    def fulfil(self, order_id: str, tracking: str, url: str, company: str) -> dict:
        r = self.http.post(f"{self.base}/orders/{order_id}/fulfillments.json", headers=self.h,
                           json={"fulfillment": {"tracking_number": tracking, "tracking_url": url, "tracking_company": company, "notify_customer": True}})
        r.raise_for_status()
        return r.json()["fulfillment"]


def products(db) -> dict:
    return {p["sku"]: dict(p) for p in db.execute("select * from products")}


def receive(db, o: dict, who: str = "shop webhook") -> str:
    """A paid Shopify order (orders/paid payload)."""
    a = o.get("shipping_address") or {}
    address = {"name": f"{a.get('first_name', '')} {a.get('last_name', '')}".strip(), "company": a.get("company") or "", "street": a.get("address1") or "",
               "extra": a.get("address2") or "", "city": a.get("city") or "", "postcode": (a.get("zip") or "").strip(), "country": (a.get("country_code") or "").upper(),
               "phone": a.get("phone") or o.get("phone") or ""}
    lines = [{"sku": l.get("sku") or "", "qty": int(l["quantity"]), "title": l.get("title", "")} for l in o.get("line_items", [])]
    shipping = "express" if any("express" in (s.get("code") or "").lower() for s in o.get("shipping_lines", [])) else "standard"
    row = {"id": str(o["id"]), "number": o.get("name") or str(o["id"]), "email": o.get("email"), "address": address, "lines": lines, "shipping": shipping,
           "total_cents": round(float(o.get("total_price") or 0) * 100)}
    if db.execute("select 1 from orders where id = ?", (row["id"],)).fetchone():
        return "duplicate"
    d = decide(row, products(db))
    status = "held" if "hold" in d else "ready"
    db.execute("""insert into orders (id, number, email, address, lines, shipping, total_cents, received_at, status, reason, carrier, service, rule, weight_g, updated_at)
                  values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
               (row["id"], row["number"], row["email"], json.dumps(address), json.dumps(lines), shipping, row["total_cents"], now(), status, d.get("hold"),
                d.get("carrier"), d.get("service"), d.get("rule"), d.get("weight_g"), now()))
    audit(db, row["id"], who, f"received: {d['hold']}" if status == "held" else f"ready: {d['rule']}")
    return status


def fix(db, order_id: str, address: dict, who: str) -> str:
    o = db.execute("select * from orders where id = ?", (order_id,)).fetchone()
    if o["status"] != "held":
        raise ValueError("Only orders on hold can be changed here.")
    merged = {**json.loads(o["address"]), **{k: v.strip() for k, v in address.items() if v is not None}}
    d = decide({"address": merged, "lines": json.loads(o["lines"]), "shipping": o["shipping"]}, products(db))
    status = "held" if "hold" in d else "ready"
    db.execute("update orders set address = ?, status = ?, reason = ?, carrier = ?, service = ?, rule = ?, weight_g = ?, updated_at = ? where id = ?",
               (json.dumps(merged), status, d.get("hold"), d.get("carrier"), d.get("service"), d.get("rule"), d.get("weight_g"), now(), order_id))
    audit(db, order_id, who, "address corrected: " + (d["hold"] if status == "held" else f"ready, {d['rule']}"))
    return status


def label(db, carrier: Carrier, order_id: str, labels_dir: Path) -> str:
    o = db.execute("select * from orders where id = ?", (order_id,)).fetchone()
    if o["status"] != "ready":
        return o["status"]
    a, lines, cat = json.loads(o["address"]), json.loads(o["lines"]), products(db)
    existing = carrier.find(o["number"])  # made before a timeout or crash: reuse it, don't buy a second label
    if existing:
        p = existing
    else:
        payload = {"name": a["name"], "company_name": a["company"], "address": a["street"], "address_2": a["extra"], "city": a["city"], "postal_code": a["postcode"],
                   "country": a["country"], "telephone": a["phone"], "email": o["email"], "order_number": o["number"], "weight": f"{o['weight_g'] / 1000:.3f}",
                   "shipment": {"id": CARRIER_IDS[o["carrier"]]}, "request_label": True}
        if a["country"] not in EU:
            payload |= {"customs_invoice_nr": o["number"], "customs_shipment_type": 2, "parcel_items": [
                {"description": cat[l["sku"]]["title"], "quantity": l["qty"], "weight": f"{cat[l['sku']]['weight_g'] / 1000:.3f}", "value": f"{cat[l['sku']]['value_cents'] / 100:.2f}",
                 "hs_code": cat[l["sku"]]["hs_code"], "origin_country": cat[l["sku"]]["origin"], "sku": l["sku"]} for l in lines]}
        r = carrier.create(payload)
        if r.status_code == 422 or r.status_code == 400:
            m = r.json().get("error", {}).get("message")
            msg = "; ".join(f"{k}: {v}" for k, v in m.items()) if isinstance(m, dict) else str(m or r.text[:200])
            db.execute("update orders set status = 'held', reason = ?, updated_at = ? where id = ?", (f"carrier refused the label: {msg}", now(), order_id))
            audit(db, order_id, "carrier", f"label refused: {msg}")
            return "held"
        r.raise_for_status()
        p = r.json()["parcel"]
    labels_dir.mkdir(parents=True, exist_ok=True)
    path = labels_dir / f"{o['number'].lstrip('#')}-{p['id']}.pdf"
    path.write_bytes(carrier.label_pdf(p["label"]["label_printer"]))
    db.execute("""insert or ignore into parcels (order_id, carrier_parcel_id, tracking, tracking_url, label_url, label_path, created_at)
                  values (?, ?, ?, ?, ?, ?, ?)""", (order_id, str(p["id"]), p["tracking_number"], p["tracking_url"], p["label"]["label_printer"], str(path), now()))
    db.execute("update orders set status = 'labelled', updated_at = ? where id = ?", (now(), order_id))
    audit(db, order_id, "carrier", f"label {p['tracking_number']} ({o['carrier']} {o['service']})" + (" reused after a retry" if existing else ""))
    return "labelled"


def batch(db, who: str, out_dir: Path) -> dict | None:
    rows = db.execute("select p.*, o.number from parcels p join orders o on o.id = p.order_id where p.printed_batch is null and p.kind = 'outbound' order by o.carrier, p.id").fetchall()
    if not rows:
        return None
    w = PdfWriter()
    for r in rows:
        w.append(r["label_path"])
    out_dir.mkdir(parents=True, exist_ok=True)
    bid = db.execute("insert into batches (at, by, parcels, path) values (?, ?, ?, '')", (now(), who, len(rows))).lastrowid
    path = out_dir / f"labels-batch-{bid}.pdf"
    with open(path, "wb") as f:
        w.write(f)
    db.execute("update batches set path = ? where id = ?", (str(path), bid))
    db.executemany("update parcels set printed_batch = ? where id = ?", [(bid, r["id"]) for r in rows])
    audit(db, None, who, f"printed batch {bid}: {len(rows)} labels")
    return {"id": bid, "path": str(path), "parcels": len(rows)}


def fulfil(db, shop: Shop, carrier_names: dict) -> int:
    n = 0
    for p in db.execute("select p.*, o.carrier from parcels p join orders o on o.id = p.order_id where p.fulfilled = 0 and p.kind = 'outbound' and p.printed_batch is not null").fetchall():
        shop.fulfil(p["order_id"], p["tracking"], p["tracking_url"], carrier_names[p["carrier"]])
        db.execute("update parcels set fulfilled = 1 where id = ?", (p["id"],))
        db.execute("update orders set status = 'shipped', updated_at = ? where id = ? and status = 'labelled'", (now(), p["order_id"]))
        audit(db, p["order_id"], "shop", f"fulfilled with tracking {p['tracking']}; the shop emails the customer")
        n += 1
    return n


PROBLEMS = {"address_unknown": "the carrier can't find the address", "refused": "the recipient refused the parcel", "damaged": "the parcel was damaged",
            "returned_to_sender": "on its way back to us"}


def track(db, ev: dict) -> str:
    p = db.execute("select * from parcels where carrier_parcel_id = ?", (str(ev["parcel_id"]),)).fetchone()
    if not p:
        return "unknown parcel"
    if db.execute("insert or ignore into events (parcel_id, code, message, at, ext_id) values (?, ?, ?, ?, ?)",
                  (p["id"], ev["status"], ev.get("message", ""), ev.get("timestamp") or now(), ev.get("id"))).rowcount == 0:
        return "duplicate"
    db.execute("update parcels set last_event = ?, last_event_at = ? where id = ?", (ev["status"], now(), p["id"]))
    if ev["status"] == "delivered":
        db.execute("update orders set status = 'delivered', reason = null, updated_at = ? where id = ?", (now(), p["order_id"]))
    elif ev["status"] in PROBLEMS:
        db.execute("update orders set status = 'exception', reason = ?, updated_at = ? where id = ?", (PROBLEMS[ev["status"]] + (f": {ev['message']}" if ev.get("message") else ""), now(), p["order_id"]))
        audit(db, p["order_id"], "carrier", f"problem: {PROBLEMS[ev['status']]}")
    return ev["status"]


def stuck(db, days: float = STUCK_DAYS) -> list[str]:
    """Shipped parcels without a scan for days: the carrier lost track of them, or never got them."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for p in db.execute("""select p.* from parcels p join orders o on o.id = p.order_id where o.status = 'shipped'
                           and coalesce(p.last_event_at, p.created_at) < ?""", (cutoff,)).fetchall():
        db.execute("update orders set status = 'exception', reason = ?, updated_at = ? where id = ?", (f"no scan for {days:g} days", now(), p["order_id"]))
        audit(db, p["order_id"], "system", f"no carrier scan for {days:g} days")
        out.append(p["order_id"])
    return out
