"""Booking rules, in one place for every channel (online widget, phone agent, SMS, staff panel).

- Tables are allocated at booking time: the smallest single table that fits, else the smallest allowed combination of
  neighbouring tables. A party never gets a table more than two seats too big, unless staff choose it by hand.
- Every booking runs in one `begin immediate` transaction: the availability check and the insert can't interleave
  with another booking, so two guests can't get the same table for the same time.
- The stay length depends on the party size; a table is free again when the stay ends.
- Messages (confirmation, reminder, cancellation, waitlist offers) go to an outbox the worker sends; nothing here
  talks to the network.
"""
import json, re, secrets, sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .db import audit, now, settings

LIVE = ("booked", "seated")
MAX_WASTE = 2  # seats a table may have beyond the party size, for automatic allocation
OPEN_PER_PHONE = 3  # open online bookings per phone number


class Invalid(Exception):
    """The request can't be a booking (bad date, outside hours, party too big). The message is safe to show or speak."""


class Unavailable(Exception):
    def __init__(self, msg: str, alternatives: list[str] | None = None):
        super().__init__(msg)
        self.alternatives = alternatives or []


@contextmanager
def tx(db: sqlite3.Connection):
    db.execute("begin immediate")
    try:
        yield
        db.execute("commit")
    except BaseException:
        db.execute("rollback")
        raise


def local_now(s: dict) -> datetime:
    return datetime.now(ZoneInfo(s["timezone"])).replace(tzinfo=None, second=0, microsecond=0)


def epoch(s: dict, local: datetime) -> float:
    return local.replace(tzinfo=ZoneInfo(s["timezone"])).timestamp()


def fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def normalize_phone(raw: str, default_cc: str = "+49") -> str:
    p = re.sub(r"[\s\-/().]", "", raw or "")
    if p.startswith("00"):
        p = "+" + p[2:]
    elif p.startswith("0"):
        p = default_cc + p[1:]
    if not re.fullmatch(r"\+\d{8,15}", p):
        raise Invalid("That phone number doesn't look right.")
    return p


def stay_minutes(s: dict, party: int) -> int:
    for up_to, minutes in sorted(((int(k), v) for k, v in s["stay_minutes"].items())):
        if party <= up_to:
            return minutes
    return max(s["stay_minutes"].values())


def seating_windows(s: dict, day: date) -> list[tuple[datetime, datetime]]:
    """(first, last) seating times for a day. Last seating is one hour before closing."""
    if day.isoformat() in s["closed_days"]:
        return []
    out = []
    for open_, close in s["hours"].get(str(day.weekday()), []):
        first = datetime.combine(day, datetime.strptime(open_, "%H:%M").time())
        last = datetime.combine(day, datetime.strptime(close, "%H:%M").time()) - timedelta(minutes=60)
        if last >= first:
            out.append((first, last))
    return out


def parse_start(s: dict, day: str, time: str, party: int, channel: str) -> datetime:
    try:
        start = datetime.strptime(f"{day} {time}", "%Y-%m-%d %H:%M")
    except ValueError:
        raise Invalid("I didn't understand the date or time.")
    limit = {"online": s["online_max_party"], "phone": s["phone_max_party"]}.get(channel, 40)
    if party < 1:
        raise Invalid("The party needs at least one person.")
    if party > limit:
        raise Invalid(f"For more than {limit} people please call us, we'll arrange it personally.")
    now_ = local_now(s)
    if start < now_:
        raise Invalid("That time has already passed.")
    if start.date() > (now_ + timedelta(days=s["booking_horizon_days"])).date():
        raise Invalid(f"We take bookings up to {s['booking_horizon_days']} days ahead.")
    windows = seating_windows(s, start.date())
    if not windows:
        raise Invalid("We're closed that day.")
    if start.minute % s["slot_minutes"] or not any(a <= start <= b for a, b in windows):
        hours = ", ".join(f"{a:%H:%M}-{b:%H:%M}" for a, b in windows)
        raise Invalid(f"Seating times that day are {hours}, every {s['slot_minutes']} minutes.")
    return start


def candidates(db, party: int, channel: str) -> list[list[str]]:
    """Table sets that seat the party, best first: least waste, then fewest tables."""
    online = {r["id"] for r in db.execute("select id from tables_ where online = 1")}
    sets = [([r["id"]], r["seats"]) for r in db.execute("select id, seats from tables_")]
    sets += [(r["tables"].split("+"), r["seats"]) for r in db.execute("select tables, seats from combos")]
    out = [(seats - party, len(ids), ids) for ids, seats in sets
           if seats >= party and (channel == "staff" or seats - party <= MAX_WASTE)
           and (channel != "online" or set(ids) <= online)]
    return [ids for *_, ids in sorted(out)]


def busy_tables(db, start: datetime, end: datetime, ignore: int | None = None) -> set[str]:
    rows = db.execute(f"""select bt.table_id from booking_tables bt join bookings b on b.id = bt.booking_id
                          where b.status in {LIVE} and b.start < ? and b.end_ > ? and b.id != ?""",
                      (fmt(end), fmt(start), ignore or 0))
    return {r[0] for r in rows}


def find_tables(db, s: dict, party: int, start: datetime, channel: str) -> list[str] | None:
    busy = busy_tables(db, start, start + timedelta(minutes=stay_minutes(s, party)))
    return next((ids for ids in candidates(db, party, channel) if not busy & set(ids)), None)


def free_times(db, day: str, party: int, channel: str = "online") -> list[str]:
    s = settings(db)
    try:
        d = date.fromisoformat(day)
    except ValueError:
        raise Invalid("I didn't understand the date.")
    out, now_ = [], local_now(s)
    for first, last in seating_windows(s, d):
        t = first
        while t <= last:
            if t >= now_ and find_tables(db, s, party, t, channel):
                out.append(f"{t:%H:%M}")
            t += timedelta(minutes=s["slot_minutes"])
    return out


def check(db, day: str, time: str, party: int, channel: str) -> dict:
    s = settings(db)
    start = parse_start(s, day, time, party, channel)
    if find_tables(db, s, party, start, channel):
        return {"available": True, "start": fmt(start)}
    times = free_times(db, day, party, channel)
    nearest = sorted(times, key=lambda t: abs((datetime.strptime(t, "%H:%M") - datetime.strptime(time, "%H:%M")).total_seconds()))[:3]
    return {"available": False, "alternatives": sorted(nearest)}


def new_code(db) -> str:
    while True:
        code = "".join(secrets.choice("ACDEFGHJKMNPQRTUVWXY3479") for _ in range(6))
        if not db.execute("select 1 from bookings where code = ?", (code,)).fetchone():
            return code


def book(db, *, name: str, phone: str, day: str, time: str, party: int, channel: str, who: str,
         email: str | None = None, notes: str = "", tables: list[str] | None = None) -> sqlite3.Row:
    s = settings(db)
    name = (name or "").strip()[:80]
    if not name:
        raise Invalid("I need a name for the booking.")
    phone = normalize_phone(phone)
    start = parse_start(s, day, time, party, channel)
    end = start + timedelta(minutes=stay_minutes(s, party))
    with tx(db):
        if channel == "online":
            open_ = db.execute(f"select count(*) from bookings where phone = ? and status in {LIVE} and start >= ?",
                               (phone, fmt(local_now(s)))).fetchone()[0]
            if open_ >= OPEN_PER_PHONE:
                raise Invalid(f"This number already has {open_} open bookings. Please call us for more.")
        if tables:  # staff picked the tables by hand
            if busy_tables(db, start, end) & set(tables):
                raise Unavailable("One of those tables is taken at that time.")
            chosen = tables
        else:
            chosen = find_tables(db, s, party, start, channel)
        if not chosen:
            raise Unavailable("Sorry, nothing is free for that time.", check(db, day, time, party, channel)["alternatives"])
        cur = db.execute("""insert into bookings (code, name, phone, email, party, start, end_, channel, notes, created_at)
                            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                         (new_code(db), name, phone, email, party, fmt(start), fmt(end), channel, (notes or "")[:500], now()))
        bid = cur.lastrowid
        db.executemany("insert into booking_tables values (?, ?)", [(bid, t) for t in chosen])
        audit(db, who, bid, "booked", f"{party} people at {fmt(start)} on {'+'.join(chosen)} via {channel}")
        b = db.execute("select * from bookings where id = ?", (bid,)).fetchone()
        queue_booking_messages(db, s, b)
    return b


def queue_booking_messages(db, s: dict, b) -> None:
    send(db, "confirmation", b["phone"], text(s, "confirmation", b), b["id"])
    start = datetime.strptime(b["start"], "%Y-%m-%d %H:%M")
    remind_at = epoch(s, start) - s["reminder_hours"] * 3600
    if remind_at > epoch(s, local_now(s)) + 3600:  # booked at short notice: the confirmation is reminder enough
        send(db, "reminder", b["phone"], text(s, "reminder", b), b["id"], due=remind_at)


def send(db, kind: str, to: str, body: str, booking_id: int | None = None, due: float | None = None) -> None:
    db.execute("insert into outbox (kind, to_, body, booking_id, due) values (?, ?, ?, ?, ?)",
               (kind, to, body, booking_id, due if due is not None else datetime.now().timestamp()))


def cancel(db, booking_id: int, who: str, notify: bool = True) -> sqlite3.Row:
    s = settings(db)
    with tx(db):
        b = db.execute("select * from bookings where id = ?", (booking_id,)).fetchone()
        if not b or b["status"] not in LIVE:
            raise Invalid("That booking isn't open.")
        db.execute("update bookings set status = 'cancelled', cancelled_at = ? where id = ?", (now(), booking_id))
        db.execute("update outbox set status = 'skipped' where booking_id = ? and status = 'queued'", (booking_id,))
        audit(db, who, booking_id, "cancelled")
        if notify:
            send(db, "cancelled", b["phone"], text(s, "cancelled", b), booking_id)
    offer_waitlist(db, b["start"][:10])
    return db.execute("select * from bookings where id = ?", (booking_id,)).fetchone()


def set_status(db, booking_id: int, status: str, who: str) -> None:
    allowed = {"seated": ("booked",), "done": ("seated",), "no_show": ("booked",), "booked": ("no_show", "seated")}
    with tx(db):
        b = db.execute("select status from bookings where id = ?", (booking_id,)).fetchone()
        if not b or b["status"] not in allowed.get(status, ()):
            raise Invalid(f"Can't change a {b['status'] if b else 'missing'} booking to {status}.")
        db.execute("update bookings set status = ? where id = ?", (status, booking_id))
        if status in ("no_show", "done"):
            db.execute("update outbox set status = 'skipped' where booking_id = ? and status = 'queued'", (booking_id,))
        audit(db, who, booking_id, status)
    if status in ("done", "no_show"):  # the table is free early: someone on the waitlist may want it
        offer_waitlist(db, db.execute("select start from bookings where id = ?", (booking_id,)).fetchone()[0][:10])


# ---- waitlist: one live offer per day, so two guests are never promised the same table ----

def join_waitlist(db, *, name: str, phone: str, day: str, earliest: str, latest: str, party: int, who: str) -> int:
    s = settings(db)
    phone = normalize_phone(phone)
    a, b = parse_start(s, day, earliest, party, "phone"), datetime.strptime(f"{day} {latest}", "%Y-%m-%d %H:%M")
    if b < a:
        raise Invalid("The latest time is before the earliest.")
    wid = db.execute("""insert into waitlist (name, phone, party, day, earliest, latest, created_at) values (?, ?, ?, ?, ?, ?, ?)""",
                     (name.strip()[:80], phone, party, day, earliest, latest, now())).lastrowid
    audit(db, who, None, "waitlist", f"#{wid} {party} people {day} {earliest}-{latest}")
    return wid


def offer_waitlist(db, day: str) -> int | None:
    s = settings(db)
    with tx(db):
        if db.execute("select 1 from waitlist where day = ? and status = 'offered'", (day,)).fetchone():
            return None
        for w in db.execute("select * from waitlist where day = ? and status = 'waiting' order by id", (day,)).fetchall():
            t = datetime.strptime(f"{day} {w['earliest']}", "%Y-%m-%d %H:%M")
            last = datetime.strptime(f"{day} {w['latest']}", "%Y-%m-%d %H:%M")
            while t <= last:
                if t >= local_now(s) and find_tables(db, s, w["party"], t, "phone"):
                    expires = datetime.now().timestamp() + s["offer_minutes"] * 60
                    db.execute("update waitlist set status = 'offered', offered_start = ?, offer_expires = ? where id = ?", (fmt(t), expires, w["id"]))
                    send(db, "offer", w["phone"], text(s, "offer", {**dict(w), "start": fmt(t)}))
                    audit(db, "system", None, "offered", f"waitlist #{w['id']} {fmt(t)}")
                    return w["id"]
                t += timedelta(minutes=s["slot_minutes"])
    return None


def expire_offers(db) -> list[str]:
    days = [r["day"] for r in db.execute("select day from waitlist where status = 'offered' and offer_expires < ?", (datetime.now().timestamp(),))]
    db.execute("update waitlist set status = 'expired' where status = 'offered' and offer_expires < ?", (datetime.now().timestamp(),))
    for d in set(days):
        offer_waitlist(db, d)
    return days


def accept_offer(db, phone: str) -> str:
    s = settings(db)
    w = db.execute("select * from waitlist where phone = ? and status = 'offered' and offer_expires >= ? order by id desc limit 1",
                   (phone, datetime.now().timestamp())).fetchone()
    if not w:
        return text(s, "no_offer", {})
    try:
        b = book(db, name=w["name"], phone=phone, day=w["day"], time=w["offered_start"][11:], party=w["party"], channel="waitlist", who="guest sms")
    except (Unavailable, Invalid):
        db.execute("update waitlist set status = 'waiting', offered_start = null, offer_expires = null where id = ?", (w["id"],))
        offer_waitlist(db, w["day"])
        return text(s, "offer_gone", {})
    db.execute("update waitlist set status = 'booked', booking_id = ? where id = ?", (b["id"], w["id"]))
    return ""  # the booking's own confirmation message goes out


# ---- inbound SMS ----

CANCEL_WORDS = {"storno", "stornieren", "absagen", "cancel", "c"}
YES_WORDS = {"ja", "yes", "j", "y", "ok"}


def handle_sms(db, from_: str, body: str, sid: str) -> str:
    """Returns the reply text ('' for none). Unknown messages go to the staff inbox, never answered by a guess."""
    s = settings(db)
    if db.execute("select 1 from inbox where sid = ?", (sid,)).fetchone():
        return ""  # the gateway retried a webhook we already handled
    try:
        phone = normalize_phone(from_)
    except Invalid:
        phone = from_
    word = re.sub(r"[^\w]", "", (body or "").strip().lower().split(" ")[0] if body.strip() else "")
    handled, bid, reply = "staff", None, ""
    if word in CANCEL_WORDS:
        b = db.execute(f"select * from bookings where phone = ? and status = 'booked' and start >= ? order by start limit 1",
                       (phone, fmt(local_now(s)))).fetchone()
        if b:
            cancel(db, b["id"], "guest sms")
            handled, bid = "cancel", b["id"]
        else:
            handled, reply = "cancel_not_found", text(s, "not_found", {})
    elif word in YES_WORDS and db.execute("select 1 from waitlist where phone = ? and status in ('offered', 'expired')", (phone,)).fetchone():
        handled, reply = "offer_accept", accept_offer(db, phone)
    db.execute("insert into inbox (from_, body, at, sid, handled_as, booking_id, done) values (?, ?, ?, ?, ?, ?, ?)",
               (phone, body[:1000], now(), sid, handled, bid, int(handled != "staff")))
    if reply:
        send(db, "reply", phone, reply)
    return reply


# ---- privacy: guests' personal data is kept only as long as it's needed ----

def anonymize(db, older_than_days: int | None = None) -> int:
    s = settings(db)
    cutoff = fmt(local_now(s) - timedelta(days=older_than_days if older_than_days is not None else s["retention_days"]))
    with tx(db):
        n = db.execute("""update bookings set name = 'Guest', phone = '', email = null, notes = null, anonymized = 1
                          where anonymized = 0 and end_ < ?""", (cutoff,)).rowcount
        db.execute("update waitlist set name = 'Guest', phone = '' where day < ? and phone != ''", (cutoff[:10],))
        db.execute("update inbox set from_ = '', body = '[removed]' where at < ? and from_ != ''", (cutoff,))
        db.execute("update callbacks set name = 'Guest', phone = '', message = '[removed]' where at < ? and phone != ''", (cutoff,))
        db.execute("update outbox set to_ = '', body = '[removed]' where status != 'queued' and sent_at < ? and to_ != ''", (cutoff,))
    return n


# ---- message texts ----

DAYS = {"de": ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"], "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]}
TEXTS = {
    "de": {
        "confirmation": "{rest}: Ihre Reservierung für {party} Pers. am {when} Uhr ist bestätigt (Code {code}). Absagen: antworten Sie STORNO.",
        "reminder": "Erinnerung von {rest}: {party} Pers. am {when} Uhr. Falls Sie nicht kommen können, antworten Sie bitte STORNO.",
        "cancelled": "{rest}: Ihre Reservierung am {when} Uhr ist storniert. Schade, bis bald!",
        "offer": "{rest}: Ein Tisch für {party} Pers. am {when} Uhr ist frei geworden. Antworten Sie JA innerhalb von {minutes} Min., dann gehört er Ihnen.",
        "offer_gone": "Leider war jemand schneller, der Tisch ist vergeben. Sie bleiben auf unserer Warteliste.",
        "no_offer": "Dieses Angebot ist leider abgelaufen. Rufen Sie uns gern an.",
        "not_found": "Zu dieser Nummer haben wir keine offene Reservierung gefunden. Bitte rufen Sie uns an.",
    },
    "en": {
        "confirmation": "{rest}: your table for {party} on {when} is confirmed (code {code}). To cancel, reply CANCEL.",
        "reminder": "Reminder from {rest}: table for {party} on {when}. If you can't make it, please reply CANCEL.",
        "cancelled": "{rest}: your booking on {when} is cancelled. Hope to see you another time!",
        "offer": "{rest}: a table for {party} on {when} just opened up. Reply YES within {minutes} min and it's yours.",
        "offer_gone": "Sorry, someone was quicker and the table is gone. You're still on our waitlist.",
        "no_offer": "That offer has expired. Please give us a call.",
        "not_found": "We couldn't find an open booking for this number. Please give us a call.",
    },
}


def text(s: dict, kind: str, b) -> str:
    b = dict(b) if b else {}
    lang = s["language"] if s["language"] in TEXTS else "en"
    when = ""
    if b.get("start"):
        dt = datetime.strptime(b["start"], "%Y-%m-%d %H:%M")
        when = f"{DAYS[lang][dt.weekday()]} {dt:%d.%m. %H:%M}" if lang == "de" else f"{DAYS[lang][dt.weekday()]} {dt:%d %b, %H:%M}"
    return TEXTS[lang][kind].format(rest=s["name"], when=when, minutes=s["offer_minutes"], party=b.get("party", ""), code=b.get("code", ""))
