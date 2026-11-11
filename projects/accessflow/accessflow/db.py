"""SQLite storage: employees mirrored from the HR system, the access each one should have and has, every action
taken in the target systems, approvals, drift findings and the audit log."""
import hashlib, hmac, json, os, secrets, sqlite3
from datetime import datetime, timezone

SCHEMA = """
create table if not exists users (id integer primary key, email text unique not null, name text not null, pw_hash text not null);
create table if not exists employees (id text primary key, name text not null, email text not null, personal_email text, github text,
  department text not null, title text, manager_id text, start_date text not null, end_date text, immediate integer not null default 0,
  updated_at text not null);
create table if not exists grants (id integer primary key, employee_id text not null references employees(id), system text not null,
  resource text not null, privileged integer not null default 0,
  state text not null check (state in ('awaiting_approval', 'granted', 'removed', 'denied')),
  requested_at text not null, approved_by text, granted_at text, removed_at text, unique (employee_id, system, resource));
create table if not exists actions (id integer primary key, employee_id text not null, system text not null, op text not null, target text not null,
  status text not null default 'pending' check (status in ('pending', 'done', 'failed')), attempts integer not null default 0, next_at real not null default 0,
  error text, created_at text not null, done_at text, detail text);
create index if not exists actions_due on actions(status, next_at);
create table if not exists drift (id integer primary key, system text not null, account text not null, detail text not null,
  status text not null default 'open', found_at text not null, resolved_by text, resolved_at text);
create unique index if not exists drift_open on drift(system, account) where status = 'open';
create table if not exists audit (id integer primary key, at text not null, employee_id text, who text not null, what text not null);
create table if not exists webhook_ids (id text primary key, at text not null);
"""

# Access by department. "account" means the person exists in that system; privileged entries need a manager's approval.
RULES = {
    "*": [("google", "account"), ("slack", "account"), ("google", "group:all@company.example"), ("slack", "group:everyone")],
    "Engineering": [("github", "account"), ("github", "team:engineering"), ("google", "group:eng@company.example"), ("slack", "group:eng")],
    "Sales": [("google", "group:sales@company.example"), ("slack", "group:sales")],
    "Finance": [("google", "group:finance@company.example"), ("slack", "group:finance")],
}
PRIVILEGED = {("github", "team:admins"), ("google", "role:admin")}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(path: str | None = None) -> sqlite3.Connection:
    db = sqlite3.connect(path or os.environ.get("DB_PATH", "accessflow.db"), timeout=15, check_same_thread=False, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("pragma journal_mode=wal")
    db.execute("pragma foreign_keys=on")
    db.executescript(SCHEMA)
    return db


def audit(db, employee_id: str | None, who: str, what: str) -> None:
    db.execute("insert into audit (at, employee_id, who, what) values (?, ?, ?, ?)", (now(), employee_id, who, what[:1000]))


def hash_pw(pw: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    return salt.hex() + "$" + hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1).hex()


def check_pw(pw: str, stored: str) -> bool:
    return hmac.compare_digest(hash_pw(pw, bytes.fromhex(stored.split("$", 1)[0])), stored)


def seed_user(db) -> None:
    db.execute("insert into users (email, name, pw_hash) values (?, ?, ?) on conflict(email) do nothing",
               ("it@example.com", "Ivy IT", hash_pw("password")))  # local demo account only
