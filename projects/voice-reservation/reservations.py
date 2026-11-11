"""Reservation core for the phone agent. Stdlib only; one SQLite file.

shortcut: one table per party (no combining tables), fixed stay length;
upgrade when a real restaurant needs table joins or per-day rules.
"""
import json
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

CFG = {
    "tz": "Europe/Berlin",
    "open": "11:30", "last": "21:00",   # placeholders until the owner confirms
    "stay_min": 75, "hold_min": 15, "max_party": 8,
    "tables": [2, 2, 2, 2, 4, 4, 4, 6],  # seats per table, placeholder
}


class Err(Exception):
    """Message is safe to speak to the caller."""


def connect(path=":memory:"):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS res(
        id INTEGER PRIMARY KEY, name TEXT, phone TEXT, start TEXT, party INTEGER,
        notes TEXT, status TEXT DEFAULT 'ok', token TEXT)""")
    return db


def _start(date, time, cfg):
    try:
        s = datetime.fromisoformat(f"{date}T{time}").replace(tzinfo=ZoneInfo(cfg["tz"]))
    except ValueError:
        raise Err("Datum oder Uhrzeit nicht verstanden")
    if s <= datetime.now(ZoneInfo(cfg["tz"])):
        raise Err("Dieser Zeitpunkt liegt in der Vergangenheit")
    if not cfg["open"] <= f"{s:%H:%M}" <= cfg["last"]:
        raise Err(f"Reservierungen sind von {cfg['open']} bis {cfg['last']} möglich")
    return s


def _free_table(db, s, party, cfg):
    end = s + timedelta(minutes=cfg["stay_min"])
    busy = [(datetime.fromisoformat(r["start"]), r["party"]) for r in
            db.execute("SELECT start,party FROM res WHERE status='ok' AND date(start)=?", (f"{s:%Y-%m-%d}",))]
    # shortcut: greedy assignment, smallest fitting table first
    free = sorted(cfg["tables"])
    for b, p in sorted(busy, key=lambda x: x[1]):
        if b < end and s < b + timedelta(minutes=cfg["stay_min"]):
            fit = next((t for t in free if t >= p), None)
            if fit:
                free.remove(fit)
    return next((t for t in free if t >= party), None)


def check_availability(db, date, time, party, cfg=CFG):
    if not 1 <= party <= cfg["max_party"]:
        raise Err(f"Per Telefon nur 1 bis {cfg['max_party']} Personen; größere Gruppen bitte per Rückruf")
    s = _start(date, time, cfg)
    return {"available": _free_table(db, s, party, cfg) is not None}


def day_slots(db, date, party, cfg=CFG):
    """Bookable times on one date, every 30 min."""
    try:
        datetime.fromisoformat(date)
    except ValueError:
        raise Err("Datum nicht verstanden")
    out, t = [], datetime.strptime(cfg["open"], "%H:%M")
    while f"{t:%H:%M}" <= cfg["last"]:
        try:
            if check_availability(db, date, f"{t:%H:%M}", party, cfg)["available"]:
                out.append(f"{t:%H:%M}")
        except Err as e:
            if "Personen" in str(e):
                raise  # bad party size applies to every slot
        t += timedelta(minutes=30)
    return out


def create_reservation(db, name, phone, date, time, party, notes="", cfg=CFG):
    if not name.strip() or not re.fullmatch(r"\+?[\d ]{6,18}", phone.strip()):
        raise Err("Name oder Telefonnummer fehlt oder ist ungültig")
    # limit per phone so one number cannot block the whole restaurant
    open_n = db.execute("SELECT count(*) FROM res WHERE status='ok' AND start>=? AND replace(phone,' ','')=?",
                        (datetime.now(ZoneInfo(cfg["tz"])).isoformat(), phone.replace(" ", ""))).fetchone()[0]
    if open_n >= 3:
        raise Err("Mit dieser Nummer gibt es schon 3 offene Reservierungen")
    # re-check inside the same call: the agent must never confirm without a row
    if not check_availability(db, date, time, party, cfg)["available"]:
        raise Err("Zu dieser Zeit ist leider kein Tisch frei")
    s = _start(date, time, cfg)
    token = secrets.token_urlsafe(8)
    cur = db.execute("INSERT INTO res(name,phone,start,party,notes,token) VALUES(?,?,?,?,?,?)",
                     (name.strip()[:80], phone.strip(), s.isoformat(), party, notes[:200], token))
    db.commit()
    return {"id": cur.lastrowid, "start": s.isoformat(), "party": party, "token": token}


def find_reservation(db, name, phone):
    rows = db.execute("SELECT id,start,party FROM res WHERE status='ok' AND phone=? AND lower(name)=lower(?) "
                      "ORDER BY start", (phone.strip(), name.strip())).fetchall()
    return {"reservations": [dict(r) for r in rows]}


def cancel_reservation(db, id, phone):
    # phone must match so a caller cannot cancel someone else's booking by guessing an id
    cur = db.execute("UPDATE res SET status='cancelled' WHERE id=? AND phone=? AND status='ok'",
                     (id, phone.strip()))
    db.commit()
    if not cur.rowcount:
        raise Err("Reservierung nicht gefunden")
    return {"cancelled": id}


def cancel_by_token(db, id, token):
    # guests cancel with the secret from their confirmation, not with their phone number
    cur = db.execute("UPDATE res SET status='cancelled' WHERE id=? AND token=? AND status='ok'", (id, token))
    db.commit()
    if not cur.rowcount:
        raise Err("Reservierung nicht gefunden")
    return {"cancelled": id}


def _outbox(db):
    db.execute("""CREATE TABLE IF NOT EXISTS outbox(
        id INTEGER PRIMARY KEY, kind TEXT, phone TEXT, text TEXT, sent INTEGER DEFAULT 0)""")


def send_sms(db, phone, text):
    # Queued only. shortcut: a sender (Twilio/sms provider) drains this table once an account exists.
    if not re.fullmatch(r"\+?[\d ]{6,18}", phone.strip()) or not text.strip():
        raise Err("SMS konnte nicht vorbereitet werden")
    _outbox(db)
    db.execute("INSERT INTO outbox(kind,phone,text) VALUES('sms',?,?)", (phone.strip(), text[:300]))
    db.commit()
    return {"queued": True}


def request_callback(db, name, phone, reason):
    # Same queue; the owner reads it as a to-do list.
    if not name.strip() or not re.fullmatch(r"\+?[\d ]{6,18}", phone.strip()):
        raise Err("Name oder Telefonnummer fehlt oder ist ungültig")
    _outbox(db)
    db.execute("INSERT INTO outbox(kind,phone,text) VALUES('callback',?,?)",
               (phone.strip(), f"{name.strip()}: {reason}"[:300]))
    db.commit()
    return {"queued": True}


TOOLS = {f.__name__: f for f in (check_availability, create_reservation, find_reservation,
                                 cancel_reservation, send_sms, request_callback)}

if __name__ == "__main__":
    print(json.dumps(CFG, indent=1))
