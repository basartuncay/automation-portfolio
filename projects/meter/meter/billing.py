"""Usage billing rules.

- Usage events are counted once: (customer, idempotency key) is unique, so a client retrying a batch never bills twice.
- Each event is reported to Stripe's meter with its own identifier, so a crash between "sent" and "marked" can't
  double-report either; Stripe drops the repeat.
- A plan includes usage; beyond it, every started 1,000 calls costs the plan's overage rate. Customers get an email at
  80% and 100% of what's included, and can set a spend cap the product enforces through the entitlement check.
- Upgrades apply at once with proration; downgrades wait for the end of the period (no refunds mid-month).
- A failed payment starts a grace period while Stripe retries the card. After it, the account is restricted until an
  invoice is paid; paying lifts it at once.
"""
import json, math, secrets
from datetime import datetime, timedelta, timezone

from .db import audit, euro, key_hash, now

GRACE_DAYS = 7
MAX_EVENT_AGE_DAYS = 35


def iso(ts: int | float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def epoch(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())


def create_customer(db, stripe, *, cid: str, name: str, email: str, plan_id: str) -> str:
    plan = db.execute("select * from plans where id = ?", (plan_id,)).fetchone()
    sc = stripe.create_customer(cid, name, email)
    sub = stripe.create_subscription(sc["id"], plan["stripe_price_base"], plan["stripe_price_metered"], cid)
    api_key = "mk_live_" + secrets.token_urlsafe(24)
    db.execute("""insert into customers (id, name, email, api_key_hash, plan_id, stripe_customer, stripe_subscription, period_start, period_end, created_at)
                  values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
               (cid, name, email, key_hash(api_key), plan_id, sc["id"], sub["id"], iso(sub["current_period_start"]), iso(sub["current_period_end"]), now()))
    audit(db, cid, "billing", f"created on {plan['name']} (Stripe {sc['id']}, {sub['id']})")
    return api_key  # shown once; only its hash is stored


def customer_by_key(db, api_key: str):
    return db.execute("select * from customers where api_key_hash = ?", (key_hash(api_key),)).fetchone()


def usage(db, c) -> int:
    return db.execute("select coalesce(sum(quantity), 0) from events where customer_id = ? and period_start = ?", (c["id"], c["period_start"])).fetchone()[0]


def projected_cents(plan, used: int) -> int:
    over = max(0, used - plan["included"])
    return plan["base_cents"] + math.ceil(over / 1000) * plan["overage_cents_per_1k"]


def ingest(db, c, events: list[dict]) -> dict:
    accepted = duplicates = 0
    rejected = []
    now_ts = datetime.now(timezone.utc).timestamp()
    for e in events[:1000]:
        key, qty, metric = str(e.get("idempotency_key") or ""), e.get("quantity"), e.get("metric")
        ts = e.get("timestamp", now_ts)
        problem = ("idempotency_key is required" if not key else "metric must be api_calls" if metric != "api_calls"
                   else "quantity must be a whole number from 1 to 1,000,000" if not isinstance(qty, int) or not 1 <= qty <= 1_000_000
                   else "timestamp is in the future" if not isinstance(ts, (int, float)) or ts > now_ts + 300
                   else f"older than {MAX_EVENT_AGE_DAYS} days" if ts < now_ts - MAX_EVENT_AGE_DAYS * 86400 else None)
        if problem:
            rejected.append({"idempotency_key": key, "error": problem})
            continue
        late = int(ts < epoch(c["period_start"]))  # usage from a closed period is billed now, and marked as such
        cur = db.execute("""insert or ignore into events (customer_id, key, metric, quantity, ts, received_at, period_start, late)
                            values (?, ?, ?, ?, ?, ?, ?, ?)""", (c["id"], key[:120], metric, qty, iso(ts), now(), c["period_start"], late))
        accepted += cur.rowcount
        duplicates += 1 - cur.rowcount
    alerts(db, c)
    return {"accepted": accepted, "duplicates": duplicates, "rejected": rejected, "used": usage(db, c)}


def entitlement(db, c) -> dict:
    plan = db.execute("select * from plans where id = ?", (c["plan_id"],)).fetchone()
    used = usage(db, c)
    cost = projected_cents(plan, used)
    if c["status"] in ("restricted", "cancelled"):
        return {"allowed": False, "reason": "account restricted: an invoice is unpaid" if c["status"] == "restricted" else "subscription cancelled",
                "used": used, "included": plan["included"], "projected_cents": cost}
    if c["spend_cap_cents"] and cost >= c["spend_cap_cents"]:
        return {"allowed": False, "reason": f"spend cap of {euro(c['spend_cap_cents'])} reached", "used": used, "included": plan["included"], "projected_cents": cost}
    return {"allowed": True, "reason": "past due: payment is being retried" if c["status"] == "past_due" else "ok",
            "used": used, "included": plan["included"], "remaining_included": max(0, plan["included"] - used), "projected_cents": cost}


def email(db, to: str, subject: str, body: str) -> None:
    db.execute("insert into outbox (to_, subject, body, at) values (?, ?, ?, ?)", (to, subject, body, now()))


def alerts(db, c) -> list[str]:
    plan = db.execute("select * from plans where id = ?", (c["plan_id"],)).fetchone()
    used, sent = usage(db, c), []
    checks = [("usage_80", used >= 0.8 * plan["included"], f"You've used 80% of the {plan['included']:,} API calls in your plan",
               f"Hi {c['name']}, you've used {used:,} of {plan['included']:,} included calls this period. Beyond that, each 1,000 calls costs {euro(plan['overage_cents_per_1k'])}."),
              ("usage_100", used >= plan["included"], "Your included API calls are used up",
               f"Hi {c['name']}, you've used all {plan['included']:,} included calls. Usage continues at {euro(plan['overage_cents_per_1k'])} per 1,000 calls; you can set a spend cap or upgrade in your billing settings.")]
    if c["spend_cap_cents"]:
        checks.append(("cap", projected_cents(plan, used) >= c["spend_cap_cents"], "Spend cap reached",
                       f"Hi {c['name']}, this period's bill reached your spend cap of {euro(c['spend_cap_cents'])}, so further calls are paused. Raise the cap to continue."))
    for kind, hit, subject, body in checks:
        if hit and db.execute("insert or ignore into alerts (customer_id, period_start, kind, at) values (?, ?, ?, ?)", (c["id"], c["period_start"], kind, now())).rowcount:
            email(db, c["email"], subject, body)
            audit(db, c["id"], "billing", f"alert: {subject}")
            sent.append(kind)
    return sent


def report(db, stripe, limit: int = 500) -> int:
    """Send unreported usage to Stripe's meter. The identifier makes a resend harmless."""
    n = 0
    for e in db.execute("""select e.*, c.stripe_customer from events e join customers c on c.id = e.customer_id
                           where e.reported = 0 order by e.id limit ?""", (limit,)).fetchall():
        stripe.meter_event(e["stripe_customer"], e["quantity"], f"{e['customer_id']}:{e['key']}", epoch(e["ts"]) if not e["late"] else epoch(e["received_at"]))
        db.execute("update events set reported = 1 where id = ?", (e["id"],))
        n += 1
    return n


def change_plan(db, stripe, cid: str, new_plan: str, who: str) -> str:
    c = db.execute("select * from customers where id = ?", (cid,)).fetchone()
    old = db.execute("select * from plans where id = ?", (c["plan_id"],)).fetchone()
    new = db.execute("select * from plans where id = ?", (new_plan,)).fetchone()
    if not new or new["id"] == old["id"]:
        raise ValueError("Pick a different plan.")
    if new["base_cents"] > old["base_cents"]:
        report(db, stripe)  # usage so far is billed at the old plan's rates
        stripe.change_prices(c["stripe_subscription"], new["stripe_price_base"], new["stripe_price_metered"], "create_prorations", f"up-{cid}-{new_plan}-{c['period_start']}")
        db.execute("update customers set plan_id = ?, pending_plan_id = null where id = ?", (new_plan, cid))
        audit(db, cid, who, f"upgraded {old['name']} → {new['name']} now, prorated")
        return f"Upgraded to {new['name']} now. The difference for the rest of this period is prorated on the next invoice."
    db.execute("update customers set pending_plan_id = ? where id = ?", (new_plan, cid))
    audit(db, cid, who, f"downgrade {old['name']} → {new['name']} scheduled for {c['period_end']}")
    return f"Moves to {new['name']} when this period ends ({c['period_end'][:10]})."


def handle_event(db, stripe, ev: dict) -> str:
    if db.execute("insert or ignore into webhook_events values (?, ?, ?)", (ev["id"], ev["type"], now())).rowcount == 0:
        return "duplicate"
    o = ev["data"]["object"]
    c = db.execute("select * from customers where stripe_customer = ?", (o.get("customer"),)).fetchone()
    if not c:
        return "unknown customer"
    t = ev["type"]
    if t.startswith("invoice."):
        db.execute("""insert into invoices (id, customer_id, number, status, amount_due, amount_paid, period_start, period_end, hosted_url, attempt_count, updated_at)
                      values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) on conflict(id) do update set status = excluded.status, amount_paid = excluded.amount_paid,
                      attempt_count = excluded.attempt_count, updated_at = excluded.updated_at""",
                   (o["id"], c["id"], o.get("number"), o["status"], o["amount_due"], o.get("amount_paid", 0), iso(o["period_start"]), iso(o["period_end"]),
                    o.get("hosted_invoice_url"), o.get("attempt_count", 0), now()))
    if t == "invoice.payment_failed" and c["status"] == "active":
        db.execute("update customers set status = 'past_due', past_due_since = ? where id = ?", (now(), c["id"]))
        email(db, c["email"], "Your payment didn't go through",
              f"Hi {c['name']}, we couldn't charge your card for invoice {o.get('number')} ({euro(o['amount_due'])}). We'll retry over the next days; "
              f"please update your card: {o.get('hosted_invoice_url')}. Your access continues for {GRACE_DAYS} days.")
        audit(db, c["id"], "stripe", f"payment failed for {o.get('number')}: past due, grace period starts")
    elif t == "invoice.paid":
        if c["status"] in ("past_due", "restricted"):
            db.execute("update customers set status = 'active', past_due_since = null where id = ?", (c["id"],))
            audit(db, c["id"], "stripe", f"{o.get('number')} paid: access restored")
        else:
            audit(db, c["id"], "stripe", f"{o.get('number')} paid ({euro(o['amount_paid'])})")
    elif t == "customer.subscription.updated":
        start, end = iso(o["current_period_start"]), iso(o["current_period_end"])
        if start != c["period_start"]:
            db.execute("update customers set period_start = ?, period_end = ? where id = ?", (start, end, c["id"]))
            audit(db, c["id"], "stripe", f"new billing period {start[:10]} – {end[:10]}")
            if c["pending_plan_id"]:
                new = db.execute("select * from plans where id = ?", (c["pending_plan_id"],)).fetchone()
                stripe.change_prices(c["stripe_subscription"], new["stripe_price_base"], new["stripe_price_metered"], "none", f"down-{c['id']}-{new['id']}-{start}")
                db.execute("update customers set plan_id = ?, pending_plan_id = null where id = ?", (new["id"], c["id"]))
                audit(db, c["id"], "billing", f"scheduled downgrade applied: now on {new['name']}")
    elif t == "customer.subscription.deleted":
        db.execute("update customers set status = 'cancelled' where id = ?", (c["id"],))
        audit(db, c["id"], "stripe", "subscription cancelled")
    return t


def enforce_grace(db, days: float = GRACE_DAYS) -> list[str]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for c in db.execute("select * from customers where status = 'past_due' and past_due_since <= ?", (cutoff,)).fetchall():
        db.execute("update customers set status = 'restricted' where id = ?", (c["id"],))
        email(db, c["email"], "Your account is paused", f"Hi {c['name']}, the invoice is still unpaid after {GRACE_DAYS} days, so API access is paused. Paying the invoice restores it at once.")
        audit(db, c["id"], "billing", f"grace period over: restricted")
        out.append(c["id"])
    return out


def reconcile(db, stripe) -> list[dict]:
    """Our count of this period's usage against Stripe's meter. Unreported events are sent first."""
    report(db, stripe)
    out = []
    for c in db.execute("select * from customers where status != 'cancelled'").fetchall():
        ours = usage(db, c)
        theirs = stripe.meter_total(c["stripe_customer"], epoch(c["period_start"]), epoch(c["period_end"]))
        out.append({"customer": c["id"], "name": c["name"], "ours": ours, "stripe": theirs, "ok": ours == theirs})
    return out
