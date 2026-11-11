"""SQLite storage: customers (with WhatsApp opt-in), conversations and their state, every message in and out with
its delivery status, processed webhook IDs, return requests, staff users."""
import hashlib, hmac, os, secrets, sqlite3
from datetime import datetime, timezone

SCHEMA = """
create table if not exists users (id integer primary key, email text unique not null, name text not null, pw_hash text not null);
create table if not exists customers (wa_id text primary key, name text, opted_in integer not null default 0, opted_in_at text, opted_out_at text,
  last_inbound real);
create table if not exists conversations (id integer primary key, wa_id text not null references customers(wa_id),
  mode text not null default 'bot' check (mode in ('bot', 'human', 'closed')), state text not null default 'menu', data text not null default '{}',
  opened_at text not null, updated_at text not null, assigned_to text, unread integer not null default 0, outcome text);
create index if not exists conv_open on conversations(wa_id, mode);
create table if not exists messages (id integer primary key, conversation_id integer not null references conversations(id),
  direction text not null check (direction in ('in', 'out')), wamid text unique, kind text not null, body text not null, payload text,
  author text not null, at text not null, status text, error text);
create table if not exists returns (id integer primary key, conversation_id integer, order_no text not null, item text not null, reason text not null,
  shop_return_id text, label_url text, at text not null);
create table if not exists statuses (wamid text primary key, status text not null, rank integer not null, error text, at text not null);
create table if not exists webhook_events (id text primary key, at text not null);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(path: str | None = None) -> sqlite3.Connection:
    db = sqlite3.connect(path or os.environ.get("DB_PATH", "chatline.db"), timeout=15, check_same_thread=False, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("pragma journal_mode=wal")
    db.execute("pragma foreign_keys=on")
    db.executescript(SCHEMA)
    return db


def hash_pw(pw: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    return salt.hex() + "$" + hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1).hex()


def check_pw(pw: str, stored: str) -> bool:
    return hmac.compare_digest(hash_pw(pw, bytes.fromhex(stored.split("$", 1)[0])), stored)


def seed_user(db) -> None:
    db.execute("insert into users (email, name, pw_hash) values (?, ?, ?) on conflict(email) do nothing",
               ("care@example.com", "Cleo Care", hash_pw("password")))  # local demo account only
