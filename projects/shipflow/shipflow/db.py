"""SQLite storage: orders from the shop with their shipping state, parcels (labels and tracking), tracking events,
the product catalogue's shipping data (weight, HS code, origin), the audit log and staff users."""
import hashlib, hmac, json, os, secrets, sqlite3
from datetime import datetime, timezone

SCHEMA = """
create table if not exists users (id integer primary key, email text unique not null, name text not null, pw_hash text not null);
create table if not exists products (sku text primary key, title text not null, weight_g integer not null, hs_code text, origin text not null default 'DE',
  value_cents integer not null);
create table if not exists orders (id text primary key, number text not null, email text, address text not null, lines text not null, shipping text not null,
  total_cents integer not null, received_at text not null,
  status text not null check (status in ('ready', 'held', 'labelled', 'shipped', 'delivered', 'exception', 'cancelled')),
  reason text, carrier text, service text, rule text, weight_g integer, updated_at text not null);
create table if not exists parcels (id integer primary key, order_id text not null references orders(id), kind text not null default 'outbound',
  carrier_parcel_id text unique, tracking text, tracking_url text, label_url text, label_path text, created_at text not null, printed_batch integer,
  fulfilled integer not null default 0, last_event text, last_event_at text);
create table if not exists events (id integer primary key, parcel_id integer not null, code text not null, message text, at text not null, ext_id text unique);
create table if not exists batches (id integer primary key, at text not null, by text not null, parcels integer not null, path text not null);
create table if not exists audit (id integer primary key, at text not null, order_id text, who text not null, what text not null);
create table if not exists webhook_ids (id text primary key, at text not null);
"""

CATALOG = [("HP-X200", "Wireless Headphones X200", 420, "851830", "CN", 12899), ("EB-PRO", "Noise-Cancelling Earbuds Pro", 120, "851830", "CN", 8899),
           ("CH-65W", "USB-C Charger 65W", 180, "850440", "CN", 3899), ("KB-K87", "Mechanical Keyboard K87", 980, "847160", "TW", 9899),
           ("SSD-1T", "Portable SSD 1TB", 90, "847170", "KR", 10899), ("DESK-L", "Standing desk frame", 32500, None, "DE", 34900)]


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(path: str | None = None) -> sqlite3.Connection:
    db = sqlite3.connect(path or os.environ.get("DB_PATH", "shipflow.db"), timeout=15, check_same_thread=False, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("pragma journal_mode=wal")
    db.execute("pragma foreign_keys=on")
    db.executescript(SCHEMA)
    db.executemany("insert or ignore into products values (?, ?, ?, ?, ?, ?)", CATALOG)
    return db


def audit(db, order_id: str | None, who: str, what: str) -> None:
    db.execute("insert into audit (at, order_id, who, what) values (?, ?, ?, ?)", (now(), order_id, who, what[:1000]))


def hash_pw(pw: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    return salt.hex() + "$" + hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1).hex()


def check_pw(pw: str, stored: str) -> bool:
    return hmac.compare_digest(hash_pw(pw, bytes.fromhex(stored.split("$", 1)[0])), stored)


def seed_user(db) -> None:
    db.execute("insert into users (email, name, pw_hash) values (?, ?, ?) on conflict(email) do nothing",
               ("warehouse@example.com", "Wim Warehouse", hash_pw("password")))  # local demo account only
