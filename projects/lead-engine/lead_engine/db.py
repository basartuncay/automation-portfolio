"""SQLite storage for the lead pipeline: leads with where each one came from, imports, the enrichment cache,
suppressions, sequences and enrollments, every email in and out, the per-lead timeline, scoring rules, users."""
import hashlib, hmac, json, os, secrets, sqlite3
from datetime import datetime, timezone

SCHEMA = """
create table if not exists users (id integer primary key, email text unique not null, name text not null, pw_hash text not null);
create table if not exists settings (key text primary key, value text not null);
create table if not exists leads (id integer primary key, email text unique not null, first_name text, last_name text, title text,
  company text, domain text, website text, country text, linkedin text,
  status text not null default 'new' check (status in ('new', 'enriched', 'qualified', 'disqualified', 'in_sequence', 'finished',
    'interested', 'not_interested', 'unsubscribed', 'bounced', 'paused')),
  reason text, score integer, score_detail text, enrichment text, email_check text, crm_id text, created_at text not null, updated_at text not null);
create index if not exists leads_status on leads(status);
create table if not exists lead_sources (id integer primary key, lead_id integer not null references leads(id), source text not null,
  import_id integer, raw text not null, at text not null);
create table if not exists imports (id integer primary key, filename text not null, fmt text not null, rows integer not null, created integer not null,
  merged integer not null, invalid integer not null, suppressed integer not null, at text not null, by text not null, report text);
create table if not exists enrich_cache (domain text primary key, data text, fetched_at real not null);
create table if not exists suppressions (value text primary key, reason text not null, at text not null, by text not null);
create table if not exists sequences (id integer primary key, name text not null, steps text not null, active integer not null default 1);
create table if not exists enrollments (id integer primary key, lead_id integer not null references leads(id), sequence_id integer not null references sequences(id),
  step integer not null default 0, status text not null default 'active' check (status in ('active', 'paused', 'finished', 'stopped')),
  next_at real not null, stop_reason text, thread_id text, created_at text not null, unique (lead_id, sequence_id));
create index if not exists enrollments_due on enrollments(status, next_at);
create table if not exists emails (id integer primary key, lead_id integer references leads(id), enrollment_id integer, step integer,
  direction text not null check (direction in ('out', 'in')), message_id text unique, in_reply_to text, subject text, body text,
  at text not null, classification text);
create table if not exists events (id integer primary key, lead_id integer not null, at text not null, kind text not null, detail text);
create index if not exists events_lead on events(lead_id, id);
create table if not exists sent_counts (day text primary key, n integer not null);
"""

RULES = [  # ideal-customer profile: transparent points, editable in the panel
    {"when": "industry", "in": ["E-commerce", "Retail", "Consumer Goods"], "points": 30, "label": "sells online"},
    {"when": "tech", "in": ["Shopify", "WooCommerce", "Shopware"], "points": 20, "label": "runs a shop platform we integrate with"},
    {"when": "employees", "between": [10, 500], "points": 20, "label": "10-500 employees"},
    {"when": "country", "in": ["DE", "AT", "CH", "NL"], "points": 10, "label": "DACH or NL"},
    {"when": "title", "contains": ["founder", "ceo", "owner", "head", "director", "operations", "cto", "geschäftsführer", "gründer", "inhaber"], "points": 20, "label": "decision maker"},
    {"when": "email", "is": "role", "points": -40, "label": "shared inbox (info@, sales@)"},
    {"when": "email", "is": "catch_all", "points": -10, "label": "domain accepts any address"},
    {"when": "email", "is": "free", "points": -30, "label": "personal email provider"},
]

SEQUENCE = [
    {"delay_days": 0, "subject": "{company} + your order flow",
     "body": "Hi {first_name},\n\nI noticed {company} runs on {tech}. Most shops your size lose a few hours a week copying orders between the shop, "
             "the warehouse and accounting.\n\nI build the integrations that remove that work, with tests and monitoring so they keep working.\n\n"
             "Worth a 15-minute call next week?\n\nBasar"},
    {"delay_days": 3, "subject": "", "body": "Hi {first_name}, a short follow-up: here's a 60-second video of a store-to-CRM sync I built, "
             "including what happens when an API is down: https://example.com/demo\n\nIf the timing is wrong, just say so.\n\nBasar"},
    {"delay_days": 4, "subject": "", "body": "Hi {first_name}, last note from me. If order or inventory work is eating your team's time later this year, "
             "reply to this and I'll pick it up from here.\n\nBasar"},
]

DEFAULTS = {
    "qualify_at": "60",
    "auto_enroll": "true",
    "daily_cap": "40",
    "send_window": json.dumps({"days": [0, 1, 2, 3, 4], "from": "08:00", "to": "17:00", "timezone": "Europe/Berlin"}),
    "sender_name": "Basar Tuncay",
    "rules": json.dumps(RULES),
}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(path: str | None = None) -> sqlite3.Connection:
    db = sqlite3.connect(path or os.environ.get("DB_PATH", "leads.db"), timeout=15, check_same_thread=False, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("pragma journal_mode=wal")
    db.execute("pragma foreign_keys=on")
    db.executescript(SCHEMA)
    db.executemany("insert into settings values (?, ?) on conflict(key) do nothing", DEFAULTS.items())
    if not db.execute("select 1 from sequences").fetchone():
        db.execute("insert into sequences (name, steps) values (?, ?)", ("Shop owners, DACH", json.dumps(SEQUENCE)))
    return db


def settings(db) -> dict:
    s = {r["key"]: r["value"] for r in db.execute("select * from settings")}
    s["rules"], s["send_window"] = json.loads(s["rules"]), json.loads(s["send_window"])
    s["qualify_at"], s["daily_cap"] = int(s["qualify_at"]), int(s["daily_cap"])
    s["auto_enroll"] = s["auto_enroll"] == "true"
    return s


def event(db, lead_id: int, kind: str, detail: str = "") -> None:
    db.execute("insert into events (lead_id, at, kind, detail) values (?, ?, ?, ?)", (lead_id, now(), kind, detail[:2000]))


def set_status(db, lead_id: int, status: str, reason: str | None = None) -> None:
    db.execute("update leads set status = ?, reason = coalesce(?, reason), updated_at = ? where id = ?", (status, reason, now(), lead_id))
    event(db, lead_id, "status", f"{status}{': ' + reason if reason else ''}")


def hash_pw(pw: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    return salt.hex() + "$" + hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1).hex()


def check_pw(pw: str, stored: str) -> bool:
    return hmac.compare_digest(hash_pw(pw, bytes.fromhex(stored.split("$", 1)[0])), stored)


def seed_user(db) -> None:
    db.execute("insert into users (email, name, pw_hash) values (?, ?, ?) on conflict(email) do nothing",
               ("growth@example.com", "Gina Growth", hash_pw("password")))  # local demo account only
