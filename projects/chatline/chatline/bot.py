"""The conversation engine: what the bot answers, when a person takes over, and the WhatsApp rules around both.

- Free-form messages are only allowed within 24 hours of the customer's last message (WhatsApp's customer service
  window). Outside it, only approved templates can be sent, and only to customers who opted in.
- The bot handles the two questions most shops get all day, "where is my order?" and "I want to return this", with
  buttons and lists instead of free text, and hands everything else to a person. While a person has the
  conversation, the bot stays silent.
- Order details are only given to the phone number the order belongs to, or after the customer confirms the
  postcode: an order number alone is not proof of anything.
- Every webhook is de-duplicated by its message ID: WhatsApp retries deliveries, and nothing may be answered twice.
"""
import json, re, time
from datetime import date, datetime

from .clients import SendError, buttons, choose, template, text
from .db import now

WINDOW = 24 * 3600
RETURN_DAYS = 30
MENU = [("track", "Track my order"), ("return", "Return an item"), ("human", "Talk to a person")]
REASONS = [("size", "Wrong size"), ("damaged", "Arrived damaged"), ("changed_mind", "Changed my mind")]
INTENTS = [("track", r"where.*order|track|tracking|status|wo ist|sendung|lieferung|paket"),
           ("return", r"return|refund|send.*back|retoure|zurück|umtausch"),
           ("human", r"human|person|agent|someone|mitarbeiter|mensch|call me|complain|beschwerde|broken|damaged|kaputt|defekt")]


def in_window(db, wa_id: str) -> bool:
    c = db.execute("select last_inbound from customers where wa_id = ?", (wa_id,)).fetchone()
    return bool(c and c["last_inbound"] and time.time() - c["last_inbound"] < WINDOW)


def deliver(db, wa, conv_id: int, wa_id: str, message: dict, author: str, log: str) -> bool:
    """Send one message and record it. Free-form messages outside the 24-hour window are refused before calling the API."""
    is_template = message["type"] == "template"
    if not is_template and not in_window(db, wa_id):
        db.execute("insert into messages (conversation_id, direction, kind, body, payload, author, at, status, error) values (?, 'out', ?, ?, ?, ?, ?, 'failed', ?)",
                   (conv_id, message["type"], log, json.dumps(message), author, now(), "outside the 24-hour window: use a template"))
        return False
    try:
        wamid = wa.send(wa_id, message)
        status, error = "sent", None
    except SendError as e:
        wamid, status, error = None, "failed", f"{e.code or ''} {e}".strip()
    db.execute("insert into messages (conversation_id, direction, wamid, kind, body, payload, author, at, status, error) values (?, 'out', ?, ?, ?, ?, ?, ?, ?, ?)",
               (conv_id, wamid, message["type"], log, json.dumps(message), author, now(), status, error))
    if wamid:
        apply_status(db, wamid)  # "delivered" may have come in while we were still waiting for the API's answer
    db.execute("update conversations set updated_at = ? where id = ?", (now(), conv_id))
    return status == "sent"


RANK = {"sent": 1, "delivered": 2, "read": 3, "failed": 4}


def record_status(db, wamid: str, status: str, error: str | None) -> None:
    """Statuses only move forward (read never goes back to delivered), and they may arrive before our own record of
    the message is written: they're kept by message ID and applied whenever the message row exists."""
    db.execute("""insert into statuses values (?, ?, ?, ?, ?) on conflict(wamid) do update set status = excluded.status, rank = excluded.rank,
                  error = coalesce(excluded.error, error), at = excluded.at where excluded.rank > statuses.rank""",
               (wamid, status, RANK.get(status, 0), error, now()))
    apply_status(db, wamid)


def apply_status(db, wamid: str) -> None:
    db.execute("""update messages set status = (select status from statuses where wamid = ?), error = coalesce((select error from statuses where wamid = ?), error)
                  where wamid = ? and exists (select 1 from statuses where wamid = ?)""", (wamid, wamid, wamid, wamid))


def open_conversation(db, wa_id: str):
    conv = db.execute("select * from conversations where wa_id = ? and mode != 'closed' order by id desc limit 1", (wa_id,)).fetchone()
    if not conv:
        cid = db.execute("insert into conversations (wa_id, opened_at, updated_at) values (?, ?, ?)", (wa_id, now(), now())).lastrowid
        conv = db.execute("select * from conversations where id = ?", (cid,)).fetchone()
    return conv


def set_state(db, conv_id: int, state: str, data: dict | None = None, mode: str | None = None, outcome: str | None = None) -> None:
    db.execute("update conversations set state = ?, data = coalesce(?, data), mode = coalesce(?, mode), outcome = coalesce(?, outcome), updated_at = ? where id = ?",
               (state, json.dumps(data) if data is not None else None, mode, outcome, now(), conv_id))


def parse(msg: dict) -> tuple[str, str, str]:
    """(kind, reply id or '', readable text) for an inbound message."""
    t = msg.get("type")
    if t == "text":
        return "text", "", msg["text"]["body"]
    if t == "interactive":
        i = msg["interactive"]
        r = i.get("button_reply") or i.get("list_reply") or {}
        return "reply", r.get("id", ""), r.get("title", "")
    if t == "button":  # a quick-reply button on a template
        return "reply", msg["button"].get("payload", ""), msg["button"].get("text", "")
    return t or "unknown", "", f"[{t}]"


def handle_webhook(db, wa, shop, payload: dict) -> dict:
    out = {"messages": 0, "statuses": 0, "duplicates": 0}
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c["wa_id"]: c.get("profile", {}).get("name") for c in value.get("contacts", [])}
            for st in value.get("statuses", []):
                err = (st.get("errors") or [{}])[0]
                record_status(db, st["id"], st["status"], f"{err.get('code')} {err.get('title')}" if st["status"] == "failed" else None)
                out["statuses"] += 1
            for msg in value.get("messages", []):
                if db.execute("insert or ignore into webhook_events values (?, ?)", (msg["id"], now())).rowcount == 0:
                    out["duplicates"] += 1
                    continue
                inbound(db, wa, shop, msg, names.get(msg["from"]))
                out["messages"] += 1
    return out


def inbound(db, wa, shop, msg: dict, name: str | None) -> None:
    wa_id = msg["from"]
    db.execute("""insert into customers (wa_id, name, last_inbound) values (?, ?, ?) on conflict(wa_id) do update
                  set name = coalesce(excluded.name, name), last_inbound = excluded.last_inbound""", (wa_id, name or None, float(msg.get("timestamp") or time.time())))
    conv = open_conversation(db, wa_id)
    kind, rid, body = parse(msg)
    db.execute("insert into messages (conversation_id, direction, wamid, kind, body, payload, author, at) values (?, 'in', ?, ?, ?, ?, 'customer', ?)",
               (conv["id"], msg["id"], kind, body[:4096], json.dumps(msg), now()))
    words = re.findall(r"\w+", body.lower())
    short = len(words) <= 3  # "Thanks! START" is a keyword; "don't stop the delivery" isn't
    if short and {"stop", "unsubscribe", "abmelden"} & set(words):
        db.execute("update customers set opted_in = 0, opted_out_at = ? where wa_id = ?", (now(), wa_id))
        deliver(db, wa, conv["id"], wa_id, text("You won't get order updates here any more. Reply START to turn them back on."), "bot", "opted out")
        return
    if short and {"start", "subscribe"} & set(words):
        db.execute("update customers set opted_in = 1, opted_in_at = ?, opted_out_at = null where wa_id = ?", (now(), wa_id))
        deliver(db, wa, conv["id"], wa_id, text("Done: you'll get shipping updates for your orders here. Reply STOP any time."), "bot", "opted in")
        return
    if conv["mode"] == "human":
        db.execute("update conversations set unread = unread + 1, updated_at = ? where id = ?", (now(), conv["id"]))
        return
    step(db, wa, shop, conv, kind, rid, body)


def menu(db, wa, conv, wa_id: str, intro: str) -> None:
    set_state(db, conv["id"], "menu", {})
    deliver(db, wa, conv["id"], wa_id, buttons(intro, MENU), "bot", f"{intro} [menu]")


def step(db, wa, shop, conv, kind: str, rid: str, body: str) -> None:
    wa_id, state, data = conv["wa_id"], conv["state"], json.loads(conv["data"])
    say = lambda m, log: deliver(db, wa, conv["id"], wa_id, m, "bot", log)
    choice = rid
    if kind == "text" and state in ("menu", "unclear"):
        choice = next((i for i, rx in INTENTS if re.search(rx, body, re.I)), "")
        if not choice:
            if state == "unclear":
                choice = "human"  # two messages the bot didn't understand: a person is faster
            else:
                set_state(db, conv["id"], "unclear", data)
                say(buttons("Sorry, I didn't get that. What can I help with?", MENU), "didn't understand [menu]")
                return
    if choice == "menu":
        return menu(db, wa, conv, wa_id, "What else can I help with?")
    if choice == "human":
        set_state(db, conv["id"], "waiting", data, mode="human", outcome="handed over")
        db.execute("update conversations set unread = unread + 1 where id = ?", (conv["id"],))
        return say(text("I've passed this to our team. A person will reply right here, usually within an hour (Mon-Fri 9-18)."), "handed to a person")
    if choice == "track":
        orders = shop.orders_for(wa_id)
        if not orders:
            set_state(db, conv["id"], "ask_order_no", data)
            return say(text("I couldn't find an order for this number. What's your order number? (e.g. 1042)"), "asked for the order number")
        if len(orders) == 1:
            return track_reply(db, wa, conv, wa_id, orders[0])
        set_state(db, conv["id"], "pick_track", {"orders": [o["number"] for o in orders]})
        return say(choose("Which order?", "Your orders", [(f"track:{o['number']}", f"Order {o['number']}", f"{o['placed'][:10]} · {o['status']}") for o in orders]), "which order? [list]")
    if choice.startswith("track:"):
        o = shop.order(choice.split(":", 1)[1])
        return track_reply(db, wa, conv, wa_id, o) if o and o["phone"] == wa_id else menu(db, wa, conv, wa_id, "That order isn't linked to this number.")
    if state == "ask_order_no" and kind == "text":
        no = re.sub(r"\D", "", body)
        o = shop.order(no) if no else None
        if not o:
            return say(text("I can't find that order number. Please check it, or tap Talk to a person."), "unknown order number")
        set_state(db, conv["id"], "ask_postcode", {"order": no})
        return say(text(f"Thanks. To keep your details safe: what's the postcode the order {no} goes to?"), "asked for the postcode")
    if state == "ask_postcode" and kind == "text":
        o = shop.order(data["order"])
        if o and re.sub(r"\s", "", body).lower() == re.sub(r"\s", "", o["postcode"]).lower():
            return track_reply(db, wa, conv, wa_id, o)
        tries = data.get("tries", 0) + 1
        if tries >= 2:
            return step(db, wa, shop, {**dict(conv), "state": "menu"}, "reply", "human", "")
        set_state(db, conv["id"], "ask_postcode", {**data, "tries": tries})
        return say(text("That postcode doesn't match. Please try again."), "postcode didn't match")
    if choice == "return":
        orders = [o for o in shop.orders_for(wa_id) if o["status"] == "delivered" and o.get("delivered")
                  and (date.today() - date.fromisoformat(o["delivered"][:10])).days <= RETURN_DAYS]
        if not orders:
            return menu(db, wa, conv, wa_id, f"I don't see a delivered order from the last {RETURN_DAYS} days on this number. Anything else?")
        set_state(db, conv["id"], "pick_return_order", {})
        return say(choose("Which order is the item from?", "Your orders",
                          [(f"ret:{o['number']}", f"Order {o['number']}", f"delivered {o['delivered'][:10]}") for o in orders]), "which order to return from? [list]")
    if choice.startswith("ret:"):
        o = shop.order(choice.split(":", 1)[1])
        if not o or o["phone"] != wa_id:
            return menu(db, wa, conv, wa_id, "That order isn't linked to this number.")
        set_state(db, conv["id"], "pick_item", {"order": o["number"]})
        return say(choose("Which item?", "Items", [(f"item:{i['sku']}", i["title"], f"€{i['price']:.2f}") for i in o["items"]]), "which item? [list]")
    if choice.startswith("item:"):
        set_state(db, conv["id"], "pick_reason", {**data, "sku": choice.split(":", 1)[1]})
        return say(buttons("Why are you sending it back?", REASONS), "reason? [buttons]")
    if state == "pick_reason" and choice in dict(REASONS):
        key = f"conv{conv['id']}-{data['order']}-{data['sku']}"
        r = shop.create_return(data["order"], data["sku"], choice, key)
        db.execute("insert into returns (conversation_id, order_no, item, reason, shop_return_id, label_url, at) values (?, ?, ?, ?, ?, ?, ?)",
                   (conv["id"], data["order"], data["sku"], choice, r["id"], r["label_url"], now()))
        set_state(db, conv["id"], "menu", {}, outcome="return created")
        extra = " Please send a photo of the damage here, it speeds up the refund." if choice == "damaged" else ""
        return say(text(f"Your return {r['id']} is registered. Free return label: {r['label_url']}\nThe refund goes out when the item arrives (usually 3-5 days).{extra}"), f"return {r['id']} created")
    return menu(db, wa, conv, wa_id, "Hi! I'm the shop assistant. What can I help with?")


def track_reply(db, wa, conv, wa_id: str, o: dict) -> bool:
    lines = {"processing": f"Order {o['number']} is being packed and should ship within 1-2 working days.",
             "shipped": f"Order {o['number']} is on its way with {o.get('carrier', 'the carrier')}. Track it here: {o.get('tracking_url', '')}",
             "delivered": f"Order {o['number']} was delivered on {(o.get('delivered') or '')[:10]}.",
             "cancelled": f"Order {o['number']} was cancelled."}
    set_state(db, conv["id"], "menu", {}, outcome="order tracked")
    return deliver(db, wa, conv["id"], wa_id, buttons(lines.get(o["status"], f"Order {o['number']}: {o['status']}."), [("menu", "Something else"), ("human", "Talk to a person")]),
                   "bot", f"order {o['number']}: {o['status']}")


# ---- people and proactive messages ----

def agent_reply(db, wa, conv_id: int, body: str, agent: str) -> bool:
    conv = db.execute("select * from conversations where id = ?", (conv_id,)).fetchone()
    db.execute("update conversations set mode = 'human', assigned_to = ?, unread = 0 where id = ?", (agent, conv_id))
    return deliver(db, wa, conv_id, conv["wa_id"], text(body), agent, body)


def close(db, conv_id: int, agent: str) -> None:
    db.execute("update conversations set mode = 'closed', unread = 0, updated_at = ?, outcome = coalesce(outcome, ?) where id = ?",
               (now(), f"closed by {agent}", conv_id))


def shipped(db, wa, order: dict) -> str:
    """The shop says an order shipped: a template message, only for customers who opted in."""
    c = db.execute("select * from customers where wa_id = ?", (order["phone"],)).fetchone()
    if not c or not c["opted_in"]:
        return "not opted in"
    conv = open_conversation(db, order["phone"])
    ok = deliver(db, wa, conv["id"], order["phone"], template("order_shipped", "en", [order["number"], order.get("carrier", ""), order.get("tracking_url", "")]),
                 "system", f"[template order_shipped] order {order['number']} shipped")
    return "sent" if ok else "failed"
