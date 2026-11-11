"""SQLite storage: bank transactions (imported once, by their bank reference), the open invoices mirrored from the
accounting system, matches with their reasons, bookings, dunning letters and the email outbox."""
import hashlib, hmac, os, secrets, sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = """
create table if not exists users (id integer primary key, email text unique not null, name text not null, pw_hash text not null);
create table if not exists imports (id integer primary key, filename text not null, fmt text not null, rows integer not null, new integer not null,
  duplicates integer not null, at text not null, by text not null);
create table if not exists transactions (id integer primary key, ref text unique not null, booked text not null, amount_cents integer not null,
  name text not null default '', iban text, purpose text not null default '', import_id integer,
  status text not null default 'new' check (status in ('new', 'suggested', 'matched', 'ignored', 'outgoing')),
  suggestions text, decided_by text, decided_at text);
create table if not exists invoices (id text primary key, number text not null, customer text not null, customer_no text, email text,
  issued text not null, due text not null, total_cents integer not null, open_cents integer not null, status text not null, synced_at text not null);
create table if not exists matches (id integer primary key, transaction_id integer not null references transactions(id), invoice_id text not null,
  amount_cents integer not null, writeoff_cents integer not null default 0, writeoff_reason text, kind text not null, score integer not null,
  reasons text not null, auto integer not null, booked integer not null default 0, booking_error text, at text not null,
  unique (transaction_id, invoice_id));
create table if not exists dunning (id integer primary key, invoice_id text not null, level integer not null, at text not null, fee_cents integer not null default 0,
  unique (invoice_id, level));
create table if not exists outbox (id integer primary key, kind text not null, to_ text not null, subject text not null, body text not null,
  status text not null default 'queued', attempts integer not null default 0, error text, sent_at text, invoice_id text);
create table if not exists settings (key text primary key, value text not null);
"""

DEFAULTS = {"auto_threshold": "80", "auto_margin": "20", "fee_tolerance_cents": "1500", "skonto_pct": "2", "skonto_days": "10",
            "dunning_days": "7,21,35", "dunning_fee_cents": "500", "company": "Nordlicht Shop GmbH"}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(path: str | None = None) -> sqlite3.Connection:
    db = sqlite3.connect(path or os.environ.get("DB_PATH", "paymatch.db"), timeout=15, check_same_thread=False, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("pragma journal_mode=wal")
    db.execute("pragma foreign_keys=on")
    db.executescript(SCHEMA)
    db.executemany("insert into settings values (?, ?) on conflict(key) do nothing", DEFAULTS.items())
    return db


def settings(db) -> dict:
    s = {r["key"]: r["value"] for r in db.execute("select * from settings")}
    out = {k: int(v) for k, v in s.items() if v.lstrip("-").isdigit()}
    out["dunning_days"] = [int(x) for x in s["dunning_days"].split(",")]
    out["company"] = s["company"]
    return out


@contextmanager
def tx(db):
    db.execute("begin immediate")
    try:
        yield
        db.execute("commit")
    except BaseException:
        db.execute("rollback")
        raise


def euro(cents: int) -> str:
    return f"€{cents / 100:,.2f}"


def hash_pw(pw: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    return salt.hex() + "$" + hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1).hex()


def check_pw(pw: str, stored: str) -> bool:
    return hmac.compare_digest(hash_pw(pw, bytes.fromhex(stored.split("$", 1)[0])), stored)


def seed_user(db) -> None:
    db.execute("insert into users (email, name, pw_hash) values (?, ?, ?) on conflict(email) do nothing",
               ("finance@example.com", "Frida Finance", hash_pw("password")))  # local demo account only
