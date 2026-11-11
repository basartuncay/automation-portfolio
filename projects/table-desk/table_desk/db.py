"""SQLite storage: tables and how they combine, opening hours, guests, bookings (with the tables each one holds),
the waitlist, the message outbox and inbox, phone-agent tool calls, staff users."""
import hashlib, hmac, json, os, secrets, sqlite3
from datetime import datetime, timezone

SCHEMA = """
create table if not exists users (id integer primary key, email text unique not null, name text not null, pw_hash text not null);
create table if not exists settings (key text primary key, value text not null);
create table if not exists tables_ (id text primary key, seats integer not null, area text not null default 'main', online integer not null default 1);
create table if not exists combos (id integer primary key, tables text not null unique, seats integer not null);
create table if not exists bookings (id integer primary key, code text unique not null, name text not null, phone text not null,
  email text, party integer not null, start text not null, end_ text not null, channel text not null, notes text,
  status text not null default 'booked' check (status in ('booked', 'seated', 'done', 'no_show', 'cancelled')),
  created_at text not null, cancelled_at text, anonymized integer not null default 0);
create index if not exists bookings_day on bookings(start);
create table if not exists booking_tables (booking_id integer not null references bookings(id), table_id text not null references tables_(id),
  primary key (booking_id, table_id));
create table if not exists waitlist (id integer primary key, name text not null, phone text not null, party integer not null,
  day text not null, earliest text not null, latest text not null, created_at text not null,
  status text not null default 'waiting' check (status in ('waiting', 'offered', 'booked', 'expired', 'cancelled')),
  offered_start text, offer_expires real, booking_id integer);
create table if not exists outbox (id integer primary key, kind text not null, to_ text not null, body text not null,
  booking_id integer, due real not null, status text not null default 'queued' check (status in ('queued', 'sent', 'failed', 'skipped')),
  attempts integer not null default 0, sid text, last_error text, sent_at text);
create index if not exists outbox_due on outbox(status, due);
create table if not exists inbox (id integer primary key, from_ text not null, body text not null, at text not null,
  sid text unique, handled_as text not null, booking_id integer, done integer not null default 0);
create table if not exists callbacks (id integer primary key, name text not null, phone text not null, message text not null,
  call_id text, at text not null, done integer not null default 0);
create table if not exists tool_calls (call_id text not null, tool text not null, args_hash text not null, response text not null,
  at text not null, primary key (call_id, tool, args_hash));
create table if not exists audit (id integer primary key, at text not null, who text not null, booking_id integer, action text not null, detail text);
"""

DEFAULTS = {
    "name": "Kiku Noodle Bar",
    "timezone": "Europe/Berlin",
    # weekday (0 = Monday) -> list of [open, close]; last seating is close minus the shortest stay
    "hours": json.dumps({str(d): [["11:30", "15:00"], ["17:00", "22:00"]] for d in range(7)} | {"0": [["17:00", "22:00"]]}),
    "closed_days": json.dumps([]),
    "slot_minutes": "15",
    "stay_minutes": json.dumps({"2": 90, "4": 105, "99": 120}),  # party size up to -> minutes
    "online_max_party": "6",
    "phone_max_party": "10",
    "booking_horizon_days": "60",
    "reminder_hours": "24",
    "offer_minutes": "15",
    "retention_days": "30",
    "language": "de",
}

TABLES = [("T1", 2, "window"), ("T2", 2, "window"), ("T3", 2, "window"), ("T4", 4, "main"), ("T5", 4, "main"),
          ("T6", 4, "main"), ("T7", 6, "main"), ("B1", 2, "bar"), ("B2", 2, "bar")]
COMBOS = [("T1+T2", 4), ("T2+T3", 4), ("T1+T2+T3", 6), ("T4+T5", 8), ("T5+T6", 8), ("T4+T5+T6", 12)]


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(path: str | None = None) -> sqlite3.Connection:
    db = sqlite3.connect(path or os.environ.get("DB_PATH", "desk.db"), timeout=15, check_same_thread=False, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("pragma journal_mode=wal")
    db.execute("pragma foreign_keys=on")
    db.executescript(SCHEMA)
    db.executemany("insert into settings values (?, ?) on conflict(key) do nothing", DEFAULTS.items())
    if not db.execute("select 1 from tables_").fetchone():
        db.executemany("insert into tables_ (id, seats, area, online) values (?, ?, ?, ?)", [(t, s, a, int(a != "bar")) for t, s, a in TABLES])
        db.executemany("insert into combos (tables, seats) values (?, ?)", COMBOS)
    return db


def settings(db: sqlite3.Connection) -> dict:
    s = {r["key"]: r["value"] for r in db.execute("select * from settings")}
    for k in ("hours", "closed_days", "stay_minutes"):
        s[k] = json.loads(s[k])
    for k in ("slot_minutes", "online_max_party", "phone_max_party", "booking_horizon_days", "reminder_hours", "offer_minutes", "retention_days"):
        s[k] = int(s[k])
    return s


def audit(db, who: str, booking_id: int | None, action: str, detail: str = "") -> None:
    db.execute("insert into audit (at, who, booking_id, action, detail) values (?, ?, ?, ?, ?)", (now(), who, booking_id, action, detail))


def hash_pw(pw: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    return salt.hex() + "$" + hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1).hex()


def check_pw(pw: str, stored: str) -> bool:
    return hmac.compare_digest(hash_pw(pw, bytes.fromhex(stored.split("$", 1)[0])), stored)


def seed_user(db) -> None:
    db.execute("insert into users (email, name, pw_hash) values (?, ?, ?) on conflict(email) do nothing",
               ("host@example.com", "Hana Host", hash_pw("password")))  # local demo account only
