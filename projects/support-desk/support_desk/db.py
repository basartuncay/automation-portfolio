"""SQLite storage shared by the intake worker and the web panel. WAL mode lets both use it at once."""
import hashlib, hmac, json, os, secrets, sqlite3
from datetime import datetime, timezone

SCHEMA = """
create table if not exists users (
  id integer primary key, email text unique not null, name text not null,
  role text not null check (role in ('viewer', 'agent', 'lead')), pw_hash text not null);
create table if not exists tickets (
  id integer primary key, message_id text unique not null,
  from_addr text not null, from_name text, subject text, body text, received_at text not null,
  category text, urgency text, summary text, injection integer not null default 0,
  status text not null check (status in ('drafted', 'needs_human', 'routed', 'sent', 'closed')),
  route text, reason text, draft text, sources text not null default '[]',
  reply text, sent_at text, sent_by integer references users(id));
create table if not exists events (
  id integer primary key, ticket_id integer not null references tickets(id), at text not null,
  actor text not null, action text not null, detail text);
create table if not exists gaps (  -- customer questions the docs could not answer: what to write next
  id integer primary key, ticket_id integer not null references tickets(id), question text not null, at text not null);
create trigger if not exists events_append_only before update on events begin select raise(abort, 'events are append-only'); end;
create trigger if not exists events_no_delete before delete on events begin select raise(abort, 'events are append-only'); end;
"""

ROLES = ("viewer", "agent", "lead")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(path: str | None = None) -> sqlite3.Connection:
    db = sqlite3.connect(path or os.environ.get("DB_PATH", "support.db"), timeout=10, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("pragma journal_mode=wal")
    db.execute("pragma foreign_keys=on")
    db.executescript(SCHEMA)
    return db


def hash_pw(pw: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    return salt.hex() + "$" + hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1).hex()


def check_pw(pw: str, stored: str) -> bool:
    salt, _ = stored.split("$", 1)
    return hmac.compare_digest(hash_pw(pw, bytes.fromhex(salt)), stored)


def add_user(db, email: str, name: str, role: str, pw: str) -> None:
    db.execute("insert into users (email, name, role, pw_hash) values (?, ?, ?, ?) on conflict(email) do nothing",
               (email.lower(), name, role, hash_pw(pw)))
    db.commit()


def seed_demo_users(db) -> None:
    for email, name, role in [("lead@example.com", "Lena Lead", "lead"), ("agent@example.com", "Sam Agent", "agent"),
                              ("viewer@example.com", "Vic Viewer", "viewer")]:
        add_user(db, email, name, role, "password")  # local demo accounts only (DEMO_SEED=true)


def log(db, ticket_id: int, actor: str, action: str, detail=None) -> None:
    db.execute("insert into events (ticket_id, at, actor, action, detail) values (?, ?, ?, ?, ?)",
               (ticket_id, now(), actor, action, json.dumps(detail) if detail is not None else None))
