"""SQLite storage: locations, reviews mirrored from Google with their analysis and reply state, the audit log."""
import hashlib, hmac, os, secrets, sqlite3
from datetime import datetime, timezone

SCHEMA = """
create table if not exists users (id integer primary key, email text unique not null, name text not null, pw_hash text not null);
create table if not exists locations (id text primary key, name text not null, contact_email text not null, last_sync text);
create table if not exists reviews (id text primary key, location_id text not null references locations(id), author text not null, stars integer not null,
  text text not null default '', lang text not null, topics text not null default '[]', urgent text, created text not null, fetched_at text not null,
  status text not null check (status in ('awaiting_approval', 'scheduled', 'posted', 'skipped', 'replied_elsewhere')),
  draft text, final text, post_after text, decided_by text, posted_at text, reply_error text);
create index if not exists reviews_status on reviews(status, post_after);
create table if not exists audit (id integer primary key, at text not null, review_id text, who text not null, what text not null);
"""
LOCATIONS = [("locations/1001", "Kiku Noodle Bar, Kaiserstraße", "hello@kiku.example"), ("locations/1002", "Kiku Noodle Bar, Südstadt", "hello@kiku.example")]


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(path: str | None = None) -> sqlite3.Connection:
    db = sqlite3.connect(path or os.environ.get("DB_PATH", "reviewdesk.db"), timeout=15, check_same_thread=False, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("pragma journal_mode=wal")
    db.executescript(SCHEMA)
    db.executemany("insert or ignore into locations (id, name, contact_email) values (?, ?, ?)", LOCATIONS)
    return db


def audit(db, review_id: str | None, who: str, what: str) -> None:
    db.execute("insert into audit (at, review_id, who, what) values (?, ?, ?, ?)", (now(), review_id, who, what[:1000]))


def hash_pw(pw: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    return salt.hex() + "$" + hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1).hex()


def check_pw(pw: str, stored: str) -> bool:
    return hmac.compare_digest(hash_pw(pw, bytes.fromhex(stored.split("$", 1)[0])), stored)


def seed_user(db) -> None:
    db.execute("insert into users (email, name, pw_hash) values (?, ?, ?) on conflict(email) do nothing",
               ("manager@example.com", "Mara Manager", hash_pw("password")))  # local demo account only
