"""SQLite storage: SKUs and bundles, channel listings with the last quantity pushed, the append-only stock ledger,
channel orders and their lines, exceptions, the sync log, reconciliation runs, cursors, users."""
import hashlib, hmac, os, secrets, sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = """
create table if not exists users (id integer primary key, email text unique not null, name text not null, pw_hash text not null);
create table if not exists skus (sku text primary key, name text not null, on_hand integer not null default 0, reserved integer not null default 0,
  low_at integer not null default 5, is_bundle integer not null default 0, check (reserved >= 0));
create table if not exists bundle_parts (bundle text not null references skus(sku), part text not null references skus(sku), qty integer not null,
  primary key (bundle, part));
create table if not exists listings (id integer primary key, channel text not null, sku text not null references skus(sku), channel_ref text not null,
  buffer integer not null default 0, max_qty integer, enabled integer not null default 1, last_pushed integer, pushed_at text, push_error text,
  unique (channel, channel_ref));
create table if not exists movements (id integer primary key, at text not null, sku text not null, kind text not null
  check (kind in ('receipt', 'reserve', 'release', 'ship', 'adjust')), qty integer not null, on_hand_after integer not null,
  reserved_after integer not null, ref text not null, note text, unique (kind, sku, ref));
create table if not exists orders (id integer primary key, channel text not null, external_id text not null, status text not null
  check (status in ('reserved', 'oversold', 'shipped', 'cancelled')), created_at text not null, updated_at text not null, unique (channel, external_id));
create table if not exists order_lines (id integer primary key, order_id integer not null references orders(id), sku text not null, qty integer not null,
  state text not null check (state in ('reserved', 'oversold', 'shipped', 'released')));
create table if not exists exceptions (id integer primary key, at text not null, kind text not null, sku text, order_id integer, detail text not null,
  status text not null default 'open', resolved_by text, resolved_at text);
create table if not exists sync_log (id integer primary key, at text not null, channel text not null, sku text, action text not null, detail text);
create table if not exists recon_runs (id integer primary key, at text not null, report text not null);
create table if not exists cursors (name text primary key, value text not null);
create table if not exists webhook_ids (id text primary key, at text not null);
"""

CATALOG = [  # sku, name, on hand, low-stock alert at
    ("MUG-BLK", "Enamel mug, black", 40, 8), ("MUG-WHT", "Enamel mug, white", 25, 8), ("BEAN-1KG", "Espresso beans 1 kg", 18, 5),
    ("GRIND-H", "Hand grinder", 6, 2), ("FILTER-100", "Paper filters (100)", 120, 20), ("KETTLE-G", "Gooseneck kettle", 3, 2),
]
BUNDLES = [("SET-STARTER", "Starter set: grinder + 1 kg beans + 100 filters", [("GRIND-H", 1), ("BEAN-1KG", 1), ("FILTER-100", 1)]),
           ("MUG-PAIR", "Mug pair, black + white", [("MUG-BLK", 1), ("MUG-WHT", 1)])]
LISTINGS = [  # channel, sku, channel reference, safety buffer, max shown
    ("shopify", "MUG-BLK", "inv_1001", 0, None), ("shopify", "MUG-WHT", "inv_1002", 0, None), ("shopify", "BEAN-1KG", "inv_1003", 0, None),
    ("shopify", "GRIND-H", "inv_1004", 0, None), ("shopify", "FILTER-100", "inv_1005", 0, 50), ("shopify", "KETTLE-G", "inv_1006", 0, None),
    ("shopify", "SET-STARTER", "inv_1007", 0, None), ("shopify", "MUG-PAIR", "inv_1008", 0, None),
    ("amazon", "MUG-BLK", "AMZ-MUG-BLK", 2, None), ("amazon", "BEAN-1KG", "AMZ-BEAN-1KG", 2, None), ("amazon", "GRIND-H", "AMZ-GRIND-H", 1, None),
    ("amazon", "KETTLE-G", "AMZ-KETTLE-G", 1, None), ("amazon", "SET-STARTER", "AMZ-SET-STARTER", 1, None),
]


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(path: str | None = None) -> sqlite3.Connection:
    db = sqlite3.connect(path or os.environ.get("DB_PATH", "stock.db"), timeout=15, check_same_thread=False, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("pragma journal_mode=wal")
    db.execute("pragma foreign_keys=on")
    db.executescript(SCHEMA)
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


def seed_catalog(db) -> None:
    if db.execute("select 1 from skus").fetchone():
        return
    with tx(db):
        for sku, name, qty, low in CATALOG:
            db.execute("insert into skus (sku, name, low_at) values (?, ?, ?)", (sku, name, low))
            db.execute("update skus set on_hand = ? where sku = ?", (qty, sku))
            db.execute("insert into movements (at, sku, kind, qty, on_hand_after, reserved_after, ref, note) values (?, ?, 'receipt', ?, ?, 0, 'opening', 'opening stock')",
                       (now(), sku, qty, qty))
        for sku, name, parts in BUNDLES:
            db.execute("insert into skus (sku, name, is_bundle, low_at) values (?, ?, 1, 2)", (sku, name))
            db.executemany("insert into bundle_parts values (?, ?, ?)", [(sku, p, q) for p, q in parts])
        db.executemany("insert into listings (channel, sku, channel_ref, buffer, max_qty) values (?, ?, ?, ?, ?)", LISTINGS)


def hash_pw(pw: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    return salt.hex() + "$" + hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1).hex()


def check_pw(pw: str, stored: str) -> bool:
    return hmac.compare_digest(hash_pw(pw, bytes.fromhex(stored.split("$", 1)[0])), stored)


def seed_user(db) -> None:
    db.execute("insert into users (email, name, pw_hash) values (?, ?, ?) on conflict(email) do nothing",
               ("ops@example.com", "Omar Ops", hash_pw("password")))  # local demo account only
