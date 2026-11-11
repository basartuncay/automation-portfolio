"""SQLite storage: plans, customers and their Stripe IDs, usage events (each counted once), what was reported to
Stripe, invoices mirrored from Stripe webhooks, alerts sent, and the audit log."""
import hashlib, hmac, os, secrets, sqlite3
from datetime import datetime, timezone

SCHEMA = """
create table if not exists users (id integer primary key, email text unique not null, name text not null, pw_hash text not null);
create table if not exists plans (id text primary key, name text not null, base_cents integer not null, included integer not null,
  overage_cents_per_1k integer not null, stripe_price_base text not null, stripe_price_metered text not null);
create table if not exists customers (id text primary key, name text not null, email text not null, api_key_hash text unique not null,
  plan_id text not null references plans(id), pending_plan_id text, stripe_customer text, stripe_subscription text,
  status text not null default 'active' check (status in ('active', 'past_due', 'restricted', 'cancelled')),
  past_due_since text, period_start text not null, period_end text not null, spend_cap_cents integer, created_at text not null);
create table if not exists events (id integer primary key, customer_id text not null references customers(id), key text not null, metric text not null,
  quantity integer not null, ts text not null, received_at text not null, period_start text not null, reported integer not null default 0,
  late integer not null default 0, unique (customer_id, key));
create index if not exists events_unreported on events(reported, id);
create table if not exists invoices (id text primary key, customer_id text not null, number text, status text not null, amount_due integer not null,
  amount_paid integer not null default 0, period_start text, period_end text, hosted_url text, attempt_count integer not null default 0, updated_at text not null);
create table if not exists alerts (id integer primary key, customer_id text not null, period_start text not null, kind text not null, at text not null,
  unique (customer_id, period_start, kind));
create table if not exists audit (id integer primary key, at text not null, customer_id text, who text not null, what text not null);
create table if not exists webhook_events (id text primary key, type text not null, at text not null);
create table if not exists outbox (id integer primary key, to_ text not null, subject text not null, body text not null, at text not null, sent integer not null default 0);
"""

PLANS = [("starter", "Starter", 2900, 10_000, 200, "price_starter_base", "price_api_calls_starter"),
         ("pro", "Pro", 9900, 100_000, 150, "price_pro_base", "price_api_calls_pro"),
         ("scale", "Scale", 49900, 1_000_000, 90, "price_scale_base", "price_api_calls_scale")]


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(path: str | None = None) -> sqlite3.Connection:
    db = sqlite3.connect(path or os.environ.get("DB_PATH", "meter.db"), timeout=15, check_same_thread=False, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("pragma journal_mode=wal")
    db.execute("pragma foreign_keys=on")
    db.executescript(SCHEMA)
    db.executemany("insert or ignore into plans values (?, ?, ?, ?, ?, ?, ?)", PLANS)
    return db


def audit(db, customer_id: str | None, who: str, what: str) -> None:
    db.execute("insert into audit (at, customer_id, who, what) values (?, ?, ?, ?)", (now(), customer_id, who, what[:1000]))


def key_hash(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def euro(cents: int | None) -> str:
    return "—" if cents is None else f"€{cents / 100:,.2f}"


def hash_pw(pw: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    return salt.hex() + "$" + hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1).hex()


def check_pw(pw: str, stored: str) -> bool:
    return hmac.compare_digest(hash_pw(pw, bytes.fromhex(stored.split("$", 1)[0])), stored)


def seed_user(db) -> None:
    db.execute("insert into users (email, name, pw_hash) values (?, ?, ?) on conflict(email) do nothing",
               ("billing@example.com", "Bo Billing", hash_pw("password")))  # local demo account only
