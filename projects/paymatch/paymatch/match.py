"""Matching incoming payments to open invoices, with a score you can read.

Every candidate collects points with a reason: the invoice number in the purpose line (also when the bank's OCR or
the payer mangled it: "RE 2026 0042", "RE20260042"), the customer number, the payer's name against the customer, and
how the amount relates to what's open (exact, early-payment discount, a bank fee short, partial). One transfer
paying several invoices of the same customer is found by subset sums. A payment is booked automatically only when
the best candidate is strong *and* clearly ahead of the next; everything else waits for a person with the top
suggestions ready.
"""
import json, re
from datetime import date
from difflib import SequenceMatcher
from itertools import combinations

from .db import euro, now, settings, tx

LEGAL = r"\b(gmbh|ug|ag|kg|ohg|e\.?k\.?|ltd|inc|co|haftungsbeschränkt|und|&)\b"


def norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(LEGAL, " ", re.sub(r"[^\w\s&]", " ", (s or "").lower()))).strip()


def name_similarity(a: str, b: str) -> float:
    a, b = norm_name(a), norm_name(b)
    if not a or not b:
        return 0.0
    ta, tb = set(a.split()), set(b.split())
    return max(SequenceMatcher(None, a, b).ratio(), len(ta & tb) / len(ta | tb))


def compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def mentions(purpose: str, number: str) -> bool:
    """Invoice number in the purpose text, ignoring spaces, dashes and a missing prefix ("2026-0042" for "RE-2026-0042")."""
    p, n = compact(purpose), compact(number)
    digits = re.sub(r"\D", "", number)
    return n in p or (len(digits) >= 6 and digits in re.sub(r"\D", " ", purpose).replace(" ", "")) or (len(digits) >= 6 and digits in p)


def score_one(t, inv, s: dict) -> dict:
    reasons, points = [], 0
    if mentions(t["purpose"], inv["number"]):
        points += 60; reasons.append(f"invoice number {inv['number']} in the purpose line")
    if inv["customer_no"] and compact(inv["customer_no"]) in compact(t["purpose"]):
        points += 15; reasons.append(f"customer number {inv['customer_no']}")
    sim = name_similarity(t["name"], inv["customer"])
    if sim >= 0.6:
        points += round(25 * sim); reasons.append(f"payer \"{t['name']}\" ≈ customer \"{inv['customer']}\" ({sim:.0%})")
    paid, open_ = t["amount_cents"], inv["open_cents"]
    days = (date.fromisoformat(t["booked"]) - date.fromisoformat(inv["issued"])).days
    discount = round(open_ * s["skonto_pct"] / 100)
    kind, alloc, writeoff, why = "partial", min(paid, open_), 0, None
    if paid == open_:
        points += 30; kind, alloc = "exact", open_; reasons.append("amount equals the open amount")
    elif paid == open_ - discount and 0 <= days <= s["skonto_days"]:
        points += 25; kind, alloc, writeoff, why = "skonto", paid, discount, f"{s['skonto_pct']}% early-payment discount"
        reasons.append(f"{s['skonto_pct']}% discount taken, paid {days} days after the invoice (allowed: {s['skonto_days']})")
    elif 0 < open_ - paid <= s["fee_tolerance_cents"] and points >= 40:
        points += 15; kind, alloc, writeoff, why = "fee", paid, open_ - paid, "bank fee deducted by the payer's bank"
        reasons.append(f"{euro(open_ - paid)} short, within the bank-fee tolerance")
    elif paid > open_:
        points += 10; kind, alloc = "overpaid", open_; reasons.append(f"{euro(paid - open_)} more than open: the rest stays as credit")
    else:
        reasons.append(f"pays {euro(paid)} of {euro(open_)} open")
    if days < 0:
        points -= 50; reasons.append("paid before the invoice date")
    return {"invoices": [inv["id"]], "numbers": [inv["number"]], "allocations": [alloc], "writeoffs": [writeoff], "writeoff_reason": why,
            "kind": kind, "score": points, "reasons": reasons}


def score_group(t, group: list, s: dict) -> dict | None:
    if sum(i["open_cents"] for i in group) != t["amount_cents"]:
        return None
    named = sum(mentions(t["purpose"], i["number"]) for i in group)
    sim = max(name_similarity(t["name"], i["customer"]) for i in group)
    points = 40 + round(60 * named / len(group)) + round(25 * sim)  # an exact sum over named invoices beats any single one
    reasons = [f"{len(group)} invoices add up exactly to {euro(t['amount_cents'])}"]
    if named:
        reasons.append(f"{named} of {len(group)} invoice numbers in the purpose line")
    if sim >= 0.6:
        reasons.append(f"payer matches the customer ({sim:.0%})")
    return {"invoices": [i["id"] for i in group], "numbers": [i["number"] for i in group], "allocations": [i["open_cents"] for i in group],
            "writeoffs": [0] * len(group), "writeoff_reason": None, "kind": "multi", "score": points, "reasons": reasons}


def candidates(db, t) -> list[dict]:
    s = settings(db)
    open_inv = db.execute("select * from invoices where open_cents > 0").fetchall()
    out = [score_one(t, i, s) for i in open_inv]
    # several invoices of one customer in one transfer: only customers the payment plausibly belongs to
    by_customer = {}
    for i in open_inv:
        if name_similarity(t["name"], i["customer"]) >= 0.6 or mentions(t["purpose"], i["number"]):
            by_customer.setdefault(i["customer"], []).append(i)
    for invs in by_customer.values():
        for k in (2, 3):
            for group in combinations(invs[:12], k):  # shortcut: 12 open invoices per customer keeps this to a few hundred sums
                g = score_group(t, list(group), s)
                if g:
                    out.append(g)
    return sorted((c for c in out if c["score"] >= 20), key=lambda c: -c["score"])[:3]  # below 20 is noise, not a lead


def run(db) -> dict:
    """Score every new payment; book the sure ones, park the rest with suggestions."""
    s = settings(db)
    auto = parked = 0
    for t in db.execute("select * from transactions where status = 'new' order by booked, id").fetchall():
        cands = candidates(db, t)
        best, second = (cands + [None, None])[:2]
        sure = best and best["score"] >= s["auto_threshold"] and (not second or best["score"] - second["score"] >= s["auto_margin"])
        if sure:
            apply(db, t["id"], best, auto=True, who="auto")
            auto += 1
        else:
            db.execute("update transactions set status = 'suggested', suggestions = ? where id = ?", (json.dumps(cands), t["id"]))
            parked += 1
    return {"auto": auto, "parked": parked}


def apply(db, tid: int, cand: dict, auto: bool, who: str) -> None:
    with tx(db):
        t = db.execute("select * from transactions where id = ?", (tid,)).fetchone()
        if t["status"] == "matched":
            return
        for inv_id, alloc, wo in zip(cand["invoices"], cand["allocations"], cand["writeoffs"]):
            inv = db.execute("select * from invoices where id = ?", (inv_id,)).fetchone()
            if alloc + wo > inv["open_cents"]:
                raise ValueError(f"{inv['number']} has only {euro(inv['open_cents'])} open now")
            db.execute("""insert into matches (transaction_id, invoice_id, amount_cents, writeoff_cents, writeoff_reason, kind, score, reasons, auto, at)
                          values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                       (tid, inv_id, alloc, wo, cand["writeoff_reason"], cand["kind"], cand["score"], json.dumps(cand["reasons"]), int(auto), now()))
            left = inv["open_cents"] - alloc - wo
            db.execute("update invoices set open_cents = ?, status = ? where id = ?", (left, "paid" if left == 0 else "partial", inv_id))
        db.execute("update transactions set status = 'matched', decided_by = ?, decided_at = ? where id = ?", (who, now(), tid))


def manual(db, tid: int, invoice_ids: list[str], who: str) -> None:
    """A person picks the invoices. The payment is spread over them oldest first; whatever's left is credit."""
    t = db.execute("select * from transactions where id = ?", (tid,)).fetchone()
    invs = sorted((db.execute("select * from invoices where id = ?", (i,)).fetchone() for i in invoice_ids), key=lambda i: i["issued"])
    left, allocs = t["amount_cents"], []
    for i in invs:
        a = min(left, i["open_cents"])
        allocs.append(a)
        left -= a
    apply(db, tid, {"invoices": [i["id"] for i in invs], "allocations": allocs, "writeoffs": [0] * len(invs), "writeoff_reason": None,
                    "kind": "manual", "score": 0, "reasons": [f"chosen by {who}"]}, auto=False, who=who)
