"""SQLite storage: service packages, clients, proposals (with the signature record), payments, provisioning steps,
the email outbox, the per-client timeline and processed webhook IDs."""
import hashlib, hmac, json, os, secrets, sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = """
create table if not exists users (id integer primary key, email text unique not null, name text not null, pw_hash text not null);
create table if not exists packages (id integer primary key, name text not null, summary text not null, base_cents integer not null,
  options text not null, weeks integer not null);
create table if not exists clients (id integer primary key, name text not null, email text not null, company text, phone text,
  brief text not null, package_id integer references packages(id), created_at text not null,
  stage text not null default 'enquiry' check (stage in ('enquiry', 'proposal_sent', 'viewed', 'signed', 'paid', 'onboarded', 'lost')));
create table if not exists proposals (id integer primary key, client_id integer not null references clients(id), version integer not null,
  token text unique not null, items text not null, total_cents integer not null, deposit_pct integer not null, valid_until text not null,
  status text not null check (status in ('draft', 'sent', 'viewed', 'signed', 'withdrawn', 'expired')),
  intro text not null default '', created_at text not null, sent_at text, viewed_at text, signed_at text, signature text, doc_sha256 text, pdf text);
create table if not exists payments (id integer primary key, proposal_id integer not null references proposals(id), session_id text unique not null,
  url text not null, amount_cents integer not null, status text not null default 'open', created_at text not null, paid_at text);
create table if not exists steps (id integer primary key, client_id integer not null references clients(id), name text not null, position integer not null,
  status text not null default 'pending' check (status in ('pending', 'done', 'failed')), attempts integer not null default 0,
  next_at real not null default 0, result text, error text, done_at text, unique (client_id, name));
create table if not exists outbox (id integer primary key, client_id integer, kind text not null, to_ text not null, subject text not null,
  body text not null, attachment text, due real not null, status text not null default 'queued' check (status in ('queued', 'sent', 'failed', 'cancelled')),
  attempts integer not null default 0, error text, sent_at text);
create table if not exists events (id integer primary key, client_id integer not null, at text not null, kind text not null, detail text);
create table if not exists webhook_events (id text primary key, at text not null);
"""

PACKAGES = [
    ("Store integration", "Connect your shop to your ERP, CRM or accounting tool, two-way, with retries and an error inbox.", 320000,
     [{"key": "monitoring", "name": "Monitoring & alerts for 3 months", "cents": 45000, "default": True},
      {"key": "extra_system", "name": "One more system connected", "cents": 90000, "default": False},
      {"key": "rush", "name": "Rush delivery (half the time)", "cents": 60000, "default": False}], 4),
    ("Automation audit", "Two workshops and a written plan: which manual work to automate first, with costs and savings.", 65000,
     [{"key": "followup", "name": "Follow-up call after 30 days", "cents": 15000, "default": False}], 1),
]


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(path: str | None = None) -> sqlite3.Connection:
    db = sqlite3.connect(path or os.environ.get("DB_PATH", "kickoff.db"), timeout=15, check_same_thread=False, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("pragma journal_mode=wal")
    db.execute("pragma foreign_keys=on")
    db.executescript(SCHEMA)
    if not db.execute("select 1 from packages").fetchone():
        db.executemany("insert into packages (name, summary, base_cents, options, weeks) values (?, ?, ?, ?, ?)",
                       [(n, s, c, json.dumps(o), w) for n, s, c, o, w in PACKAGES])
    return db


@contextmanager
def tx(db):
    db.execute("begin immediate")
    try:
        yield
        db.execute("commit")
    except BaseException:
        db.execute("rollback")
        raise


def event(db, client_id: int, kind: str, detail: str = "") -> None:
    db.execute("insert into events (client_id, at, kind, detail) values (?, ?, ?, ?)", (client_id, now(), kind, detail[:1000]))


def money(cents: int) -> str:
    return f"€{cents / 100:,.2f}"


def hash_pw(pw: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    return salt.hex() + "$" + hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1).hex()


def check_pw(pw: str, stored: str) -> bool:
    return hmac.compare_digest(hash_pw(pw, bytes.fromhex(stored.split("$", 1)[0])), stored)


def seed_user(db) -> None:
    db.execute("insert into users (email, name, pw_hash) values (?, ?, ?) on conflict(email) do nothing",
               ("studio@example.com", "Sam Studio", hash_pw("password")))  # local demo account only
