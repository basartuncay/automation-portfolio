"""The sync engine: one event in (a changed customer on either side), the right writes out.

For each linked pair of records the hub keeps a snapshot of both sides as of the last sync. That gives three guarantees:
- echo suppression: our own write coming back as a webhook or a poll result changes nothing, so no ping-pong
- only changed fields are written, never a full overwrite
- read-before-write conflict detection: if the target changed the same field since the last sync, nothing is
  overwritten; the conflict goes to a person in the panel
"""
import json, re

from . import db as store
from .clients import Permanent

# field: (direction, note). "both" = two-way; "shop>crm" / "crm>shop" = one side owns it.
FIELDS = {
    "email": ("both", "match key"),
    "first_name": ("both", ""),
    "last_name": ("both", ""),
    "phone": ("both", "normalised to +digits"),
    "total_spent": ("shop>crm", "the store is the source of truth for money"),
    "lifecycle": ("shop>crm", "customer once they've ordered, else lead"),
    "vip": ("crm>shop", "sales marks VIPs in the CRM; the store gets a VIP tag"),
}


def norm_phone(p) -> str | None:
    if not p:
        return None
    digits = re.sub(r"[^\d+]", "", str(p))
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    return digits if digits.startswith("+") else "+" + digits


def from_shop(c: dict) -> dict:
    tags = {t.strip().lower() for t in (c.get("tags") or "").split(",") if t.strip()}
    return {"email": (c.get("email") or "").strip().lower() or None, "first_name": c.get("first_name") or None,
            "last_name": c.get("last_name") or None, "phone": norm_phone(c.get("phone")),
            "total_spent": f"{float(c.get('total_spent') or 0):.2f}",
            "lifecycle": "customer" if int(c.get("orders_count") or 0) > 0 else "lead", "vip": "vip" in tags}


def from_crm(c: dict) -> dict:
    p = c.get("properties", {})
    spent = p.get("total_spent")
    return {"email": (p.get("email") or "").strip().lower() or None, "first_name": p.get("firstname") or None,
            "last_name": p.get("lastname") or None, "phone": norm_phone(p.get("phone")),
            "total_spent": f"{float(spent):.2f}" if spent not in (None, "") else None,
            "lifecycle": p.get("lifecyclestage") or None, "vip": str(p.get("vip", "")).lower() == "true"}


def to_crm(fields: dict) -> dict:
    names = {"email": "email", "first_name": "firstname", "last_name": "lastname", "phone": "phone",
             "total_spent": "total_spent", "lifecycle": "lifecyclestage", "vip": "vip"}
    return {names[k]: (str(v).lower() if isinstance(v, bool) else v) for k, v in fields.items()}


def to_shop(fields: dict, current_tags: str = "") -> dict:
    out = {k: v for k, v in fields.items() if k in ("email", "first_name", "last_name", "phone")}
    if "vip" in fields:
        tags = [t.strip() for t in current_tags.split(",") if t.strip() and t.strip().lower() != "vip"]
        out["tags"] = ", ".join(tags + (["VIP"] if fields["vip"] else []))
    return out


def flows(field: str, source: str) -> bool:
    d = FIELDS[field][0]
    return d == "both" or d == f"{source}>{'crm' if source == 'shop' else 'shop'}"


def log(conn, event_id, link_id, direction, action, detail=None) -> None:
    conn.execute("insert into log (at, event_id, link_id, direction, action, detail) values (?, ?, ?, ?, ?, ?)",
                 (store.now(), event_id, link_id, direction, action, json.dumps(detail) if detail is not None else None))


def save_link(conn, link_id, **cols) -> None:
    sets = ", ".join(f"{k} = ?" for k in cols) + ", updated_at = ?"
    conn.execute(f"update links set {sets} where id = ?", (*[json.dumps(v) if isinstance(v, dict) else v for v in cols.values()], store.now(), link_id))


def process(conn, crm, shop, event) -> str:
    """Apply one event. Raises Retryable/Permanent from the clients; the worker decides about retries."""
    source, payload, eid = event["source"], json.loads(event["payload"]), event["id"]
    target = "crm" if source == "shop" else "shop"
    arrow = f"{source}→{target}"
    rec_id = str(payload["id"])
    # webhooks/polls can be stale (retries, out-of-order delivery): always act on the source's current state
    current = shop.get(rec_id) if source == "shop" else crm.get(rec_id)
    canon = from_shop(current) if source == "shop" else from_crm(current)
    if not canon["email"]:
        raise Permanent(f"{source} record {rec_id} has no email: it can't be matched")

    link = conn.execute(f"select * from links where {source}_id = ?", (rec_id,)).fetchone()
    if link is None:
        other = crm.find_by_email(canon["email"]) if target == "crm" else shop.find_by_email(canon["email"])
        if other is None:  # new on one side: create it on the other with everything that flows that way
            fields = {f: v for f, v in canon.items() if flows(f, source) and v not in (None, False)}
            made = crm.create(to_crm(fields)) if target == "crm" else shop.create(to_shop(fields))
            made_canon = from_crm(made) if target == "crm" else from_shop(made)
            lid = conn.execute(f"""insert into links (email, {source}_id, {target}_id, {source}_snap, {target}_snap, created_at, updated_at)
                                   values (?, ?, ?, ?, ?, ?, ?)""",
                               (canon["email"], rec_id, str(made["id"]), json.dumps(canon), json.dumps(made_canon), store.now(), store.now())).lastrowid
            log(conn, eid, lid, arrow, "created", {"fields": fields})
            return "created"
        # same email exists on both sides: link them, treating the other side's values as the baseline
        other_canon = from_crm(other) if target == "crm" else from_shop(other)
        lid = conn.execute(f"""insert into links (email, {source}_id, {target}_id, {source}_snap, {target}_snap, created_at, updated_at)
                               values (?, ?, ?, ?, ?, ?, ?)""",
                           (canon["email"], rec_id, str(other["id"]), json.dumps(other_canon), json.dumps(other_canon), store.now(), store.now())).lastrowid
        log(conn, eid, lid, arrow, "linked", {"email": canon["email"]})
        link = conn.execute("select * from links where id = ?", (lid,)).fetchone()

    snap_src, snap_tgt = json.loads(link[f"{source}_snap"]), json.loads(link[f"{target}_snap"])
    changed = {f: v for f, v in canon.items() if flows(f, source) and snap_src.get(f) != v}
    if not changed:
        save_link(conn, link["id"], **{f"{source}_snap": canon})
        log(conn, eid, link["id"], arrow, "no change", None)  # typically the echo of our own write
        return "noop"

    tgt_raw = crm.get(link["crm_id"]) if target == "crm" else shop.get(link["shop_id"])
    tgt_now = from_crm(tgt_raw) if target == "crm" else from_shop(tgt_raw)
    patch, conflicts = {}, []
    for f, v in changed.items():
        if tgt_now.get(f) == v:
            continue  # already equal (both sides made the same change)
        if FIELDS[f][0] == "both" and tgt_now.get(f) != snap_tgt.get(f):
            conflicts.append(f)  # both sides changed it since the last sync, to different values
        else:
            patch[f] = v
    for f in conflicts:
        shop_v, crm_v = (canon[f], tgt_now[f]) if source == "shop" else (tgt_now[f], canon[f])
        if not conn.execute("select 1 from conflicts where link_id = ? and field = ? and status = 'open'", (link["id"], f)).fetchone():
            conn.execute("insert into conflicts (link_id, field, shop_value, crm_value, at) values (?, ?, ?, ?, ?)",
                         (link["id"], f, json.dumps(shop_v), json.dumps(crm_v), store.now()))
    # Advance the target's snapshot ONLY for fields now in sync. The target may hold its own unsynced edits in other
    # fields (its event hasn't been processed yet); absorbing them here would make that event look like "no change"
    # and silently lose the edit.
    new_tgt = dict(snap_tgt)
    for f, v in changed.items():
        if tgt_now.get(f) == v:
            new_tgt[f] = v
    if patch:
        written = crm.update(link["crm_id"], to_crm(patch)) if target == "crm" else shop.update(link["shop_id"], to_shop(patch, tgt_raw.get("tags", "")))
        after = from_crm(written) if target == "crm" else from_shop(written)
        new_tgt.update({f: after[f] for f in patch})
    new_src = dict(canon)
    for f in conflicts:  # keep the old baseline for conflicted fields so they stay visible as unsynced
        new_src[f] = snap_src.get(f)
    save_link(conn, link["id"], **{f"{source}_snap": new_src, f"{target}_snap": new_tgt})
    if patch:
        log(conn, eid, link["id"], arrow, "updated", {f: [snap_src.get(f), v] for f, v in patch.items()})
    if conflicts:
        log(conn, eid, link["id"], arrow, "conflict", {f: {"shop": (canon if source == "shop" else tgt_now)[f],
                                                          "crm": (canon if source == "crm" else tgt_now)[f]} for f in conflicts})
    return "conflict" if conflicts else "updated"


def resolve(conn, crm, shop, conflict_id: int, winner: str, user: str) -> None:
    """A person picked a side: write that value to the other side and to both snapshots."""
    c = conn.execute("select * from conflicts where id = ? and status = 'open'", (conflict_id,)).fetchone()
    if not c:
        return
    link = conn.execute("select * from links where id = ?", (c["link_id"],)).fetchone()
    value = json.loads(c[f"{winner}_value"])
    loser = "crm" if winner == "shop" else "shop"
    if loser == "crm":
        written = from_crm(crm.update(link["crm_id"], to_crm({c["field"]: value})))
    else:
        raw = shop.get(link["shop_id"])
        written = from_shop(shop.update(link["shop_id"], to_shop({c["field"]: value}, raw.get("tags", ""))))
    snaps = {s: json.loads(link[f"{s}_snap"]) for s in ("shop", "crm")}
    snaps[loser] = written
    snaps[winner][c["field"]] = value
    save_link(conn, link["id"], shop_snap=snaps["shop"], crm_snap=snaps["crm"])
    conn.execute("update conflicts set status = 'resolved', resolved_by = ?, resolution = ? where id = ?",
                 (user, f"{winner} value kept: {value}", conflict_id))
    log(conn, None, link["id"], f"{winner}→{loser}", "resolved", {c["field"]: value, "by": user})
    conn.commit()
