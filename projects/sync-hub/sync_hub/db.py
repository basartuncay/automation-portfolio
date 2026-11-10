"""SQLite storage for the hub: connections (tokens encrypted at rest), record links with per-side snapshots, the event
queue with retry state, the sync log and open conflicts."""
import base64, hashlib, hmac, os, secrets, sqlite3
from datetime import datetime, timezone

from cryptography.fernet import Fernet

SCHEMA = """
create table if not exists users (id integer primary key, email text unique not null, name text not null, pw_hash text not null);
create table if not exists connections (name text primary key, status text not null, access_token text, refresh_token text,
  expires_at real, cursor text, connected_at text, last_error text);
create table if not exists links (id integer primary key, email text not null, shop_id text unique, crm_id text unique,
  shop_snap text not null default '{}', crm_snap text not null default '{}', created_at text not null, updated_at text not null);
create table if not exists events (id integer primary key, source text not null, external_id text unique not null,
  entity text not null, topic text not null, payload text not null, received_at text not null,
  status text not null default 'queued' check (status in ('queued', 'done', 'dead')),
  attempts integer not null default 0, next_at real not null default 0, last_error text, done_at text);
create index if not exists events_due on events(status, next_at, id);
create table if not exists log (id integer primary key, at text not null, event_id integer, link_id integer, direction text,
  action text not null, detail text);
create table if not exists conflicts (id integer primary key, link_id integer not null references links(id), field text not null,
  shop_value text, crm_value text, at text not null, status text not null default 'open', resolved_by text, resolution text);
create table if not exists meta (key text primary key, value text);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(path: str | None = None) -> sqlite3.Connection:
    db = sqlite3.connect(path or os.environ.get("DB_PATH", "hub.db"), timeout=15, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("pragma journal_mode=wal")
    db.execute("pragma foreign_keys=on")
    db.executescript(SCHEMA)
    return db


def fernet() -> Fernet:
    """OAuth tokens are encrypted with a key from the environment, so a copied database file doesn't leak them."""
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(os.environ["TOKEN_KEY"].encode()).digest()))


def seal(value: str | None) -> str | None:
    return fernet().encrypt(value.encode()).decode() if value else None


def unseal(value: str | None) -> str | None:
    return fernet().decrypt(value.encode()).decode() if value else None


def hash_pw(pw: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    return salt.hex() + "$" + hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1).hex()


def check_pw(pw: str, stored: str) -> bool:
    return hmac.compare_digest(hash_pw(pw, bytes.fromhex(stored.split("$", 1)[0])), stored)


def seed_user(db) -> None:
    db.execute("insert into users (email, name, pw_hash) values (?, ?, ?) on conflict(email) do nothing",
               ("ops@example.com", "Ola Ops", hash_pw("password")))  # local demo account only (DEMO_SEED=true)
    db.commit()
