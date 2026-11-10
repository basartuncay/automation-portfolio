"""SQLite storage shared by the watcher and the panel (WAL mode, so both can use it at once)."""
import csv, hashlib, hmac, json, os, secrets, sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
create table if not exists users (id integer primary key, email text unique not null, name text not null, pw_hash text not null);
create table if not exists competitors (id integer primary key, name text unique not null, start_url text not null,
  min_products integer not null default 1);
create table if not exists our_products (sku text primary key, title text not null, pid text not null, our_price real not null,
  currency text not null default 'EUR');
create table if not exists products (  -- latest known state per competitor listing
  id integer primary key, competitor_id integer not null references competitors(id), pid text not null,
  title text not null, url text not null, price real not null, currency text not null, in_stock integer not null,
  active integer not null default 1, first_seen text not null, last_seen text not null, unique (competitor_id, pid));
create table if not exists prices (  -- history: one row per observed change
  id integer primary key, product_id integer not null references products(id), at text not null,
  price real not null, in_stock integer not null);
create table if not exists runs (id integer primary key, competitor_id integer not null references competitors(id),
  started text not null, finished text, status text, products integer, changes integer, errors integer, note text);
create table if not exists alerts (id integer primary key, at text not null, kind text not null, competitor text,
  pid text, title text, message text not null, notified integer not null default 0);
create table if not exists requests (id integer primary key, at text not null, kind text not null, done integer not null default 0);
create table if not exists meta (key text primary key, value text);
create index if not exists prices_by_product on prices(product_id, at);
"""


SYMBOLS = {"EUR": "€", "GBP": "£", "USD": "$"}


def money(v: float, currency: str = "EUR") -> str:
    sym = SYMBOLS.get(currency)
    return f"{sym}{v:,.2f}" if sym else f"{v:,.2f} {currency}"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(path: str | None = None) -> sqlite3.Connection:
    db = sqlite3.connect(path or os.environ.get("DB_PATH", "watch.db"), timeout=10)
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


def load_config(db, competitors_json: Path, catalog_csv: Path) -> None:
    """Competitors and our own catalogue come from files the client edits; re-loading is idempotent."""
    for c in json.loads(competitors_json.read_text()):
        db.execute("""insert into competitors (name, start_url, min_products) values (?, ?, ?) on conflict(name)
                      do update set start_url = excluded.start_url, min_products = excluded.min_products""",
                   (c["name"], c["start_url"], c.get("min_products", 1)))
    with open(catalog_csv, newline="") as f:
        for r in csv.DictReader(f):
            db.execute("""insert into our_products (sku, title, pid, our_price, currency) values (?, ?, ?, ?, ?) on conflict(sku)
                          do update set title = excluded.title, pid = excluded.pid, our_price = excluded.our_price,
                          currency = excluded.currency""",
                       (r["sku"], r["title"], r["pid"], float(r["our_price"]), (r.get("currency") or "EUR").upper()))
    db.commit()


def seed_demo_user(db) -> None:
    db.execute("insert into users (email, name, pw_hash) values (?, ?, ?) on conflict(email) do nothing",
               ("pricing@example.com", "Pat Pricing", hash_pw("password")))  # local demo account only (DEMO_SEED=true)
    db.commit()
