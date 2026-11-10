"""Extract and load: pull each source incrementally and land it, untouched, in DuckDB's raw schema.

- Raw is append-only with a batch id. Deduplication and typing happen later, in dbt, where they can be tested.
- Every incremental pull re-reads an overlap window (2 days for the APIs, 7 for ad platforms, which restate recent
  days), because late updates are the rule: a refund days after the order, a corrected spend figure.
- Ad exports replace their window atomically, so a re-run never double-counts spend.
- A CSV whose columns don't match what the loader expects stops that source with a clear error instead of loading
  shifted or missing data.
"""
import csv, io, os, time
from datetime import date, datetime, timedelta, timezone

import httpx

RAW_DDL = """
create schema if not exists raw;
create schema if not exists meta;
create table if not exists raw.shop_orders (id bigint, updated_at varchar, payload json, _batch varchar, _loaded_at timestamp);
create table if not exists raw.stripe_charges (id varchar, created bigint, payload json, _batch varchar, _loaded_at timestamp);
create table if not exists raw.stripe_refunds (id varchar, created bigint, payload json, _batch varchar, _loaded_at timestamp);
create table if not exists raw.ad_spend (platform varchar, day date, campaign varchar, spend double, clicks integer,
  impressions integer, _batch varchar, _loaded_at timestamp);
create table if not exists meta.state (source varchar primary key, cursor varchar);
"""

CSV_COLUMNS = {  # each platform's export headers -> our names
    "google": {"Day": "day", "Campaign": "campaign", "Cost": "spend", "Clicks": "clicks", "Impr.": "impressions"},
    "meta": {"date_start": "day", "campaign_name": "campaign", "amount_spent": "spend", "clicks": "clicks", "impressions": "impressions"},
}


class SourceError(Exception):
    pass


class SchemaDrift(SourceError):
    pass


def get(http: httpx.Client, url: str, **kw) -> httpx.Response:
    """GET with retries on 429/5xx/network errors; other errors fail the source at once."""
    tries, wait = int(os.environ.get("HTTP_TRIES", 4)), float(os.environ.get("HTTP_BACKOFF", 1.0))
    for attempt in range(1, tries + 1):
        try:
            r = http.get(url, **kw)
            if r.status_code < 400:
                return r
            if r.status_code != 429 and r.status_code < 500:
                raise SourceError(f"{url}: {r.status_code} {r.text[:120]}")
            err = f"{r.status_code}"
            delay = float(r.headers.get("Retry-After", wait * 2 ** (attempt - 1)))
        except httpx.HTTPError as e:
            err, delay = type(e).__name__, wait * 2 ** (attempt - 1)
        if attempt == tries:
            raise SourceError(f"{url}: still failing after {tries} tries ({err})")
        time.sleep(delay)


def cursor(db, source: str) -> str | None:
    r = db.execute("select cursor from meta.state where source = ?", [source]).fetchone()
    return r[0] if r else None


def set_cursor(db, source: str, value) -> None:
    db.execute("insert or replace into meta.state values (?, ?)", [source, str(value)])


def load_shop(db, http, base: str, token: str, batch: str) -> int:
    cur = cursor(db, "shop")
    since = (datetime.fromisoformat(cur.replace("Z", "+00:00")) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ") if cur else ""
    url, params, n, newest = f"{base}/shop/admin/api/2026-10/orders.json", {"updated_at_min": since, "limit": 250}, 0, cur or ""
    while url:
        r = get(http, url, params=params, headers={"X-Shopify-Access-Token": token})
        rows = r.json()["orders"]
        if rows:
            db.executemany("insert into raw.shop_orders values (?, ?, ?, ?, now())",
                           [[o["id"], o["updated_at"], json_dump(o), batch] for o in rows])
            n += len(rows)
            newest = max(newest, max(o["updated_at"] for o in rows))
        nxt = r.links.get("next", {}).get("url")
        url, params = nxt, None
    if newest:
        set_cursor(db, "shop", newest)
    return n


def load_stripe(db, http, base: str, token: str, kind: str, batch: str) -> int:
    cur = cursor(db, f"stripe_{kind}")
    gte = int(cur) - 2 * 86400 if cur else 0
    n, newest, after = 0, int(cur or 0), ""
    while True:
        params = {"created[gte]": gte, "limit": 100} | ({"starting_after": after} if after else {})
        page = get(http, f"{base}/stripe/v1/{kind}", params=params, headers={"Authorization": f"Bearer {token}"}).json()
        rows = page["data"]
        if rows:
            db.executemany(f"insert into raw.stripe_{kind} values (?, ?, ?, ?, now())", [[x["id"], x["created"], json_dump(x), batch] for x in rows])
            n += len(rows)
            newest, after = max(newest, max(x["created"] for x in rows)), rows[-1]["id"]
        if not page["has_more"]:
            break
    set_cursor(db, f"stripe_{kind}", newest)
    return n


def parse_ads_csv(platform: str, text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    want = CSV_COLUMNS[platform]
    missing = [c for c in want if c not in (reader.fieldnames or [])]
    if missing:
        raise SchemaDrift(f"{platform} export changed: missing column(s) {missing}, got {reader.fieldnames}. Update the mapping, then re-run.")
    out = []
    for row in reader:
        r = {want[k]: row[k] for k in want}
        out.append({"day": date.fromisoformat(r["day"]), "campaign": r["campaign"], "spend": float(r["spend"]),
                    "clicks": int(r["clicks"]), "impressions": int(r["impressions"])})
    return out


def load_ads(db, http, base: str, token: str, platform: str, batch: str) -> int:
    cur = cursor(db, f"ads_{platform}")
    since = (date.fromisoformat(cur) - timedelta(days=7)).isoformat() if cur else ""
    rows = parse_ads_csv(platform, get(http, f"{base}/ads/{platform}.csv", params={"since": since},
                                       headers={"Authorization": f"Bearer {token}"}).text)
    db.execute("begin")
    try:  # the restatement window is replaced as a whole: no double counting on re-runs
        db.execute("delete from raw.ad_spend where platform = ? and day >= ?", [platform, since or "1900-01-01"])
        db.executemany("insert into raw.ad_spend values (?, ?, ?, ?, ?, ?, ?, now())",
                       [[platform, r["day"], r["campaign"], r["spend"], r["clicks"], r["impressions"], batch] for r in rows])
        if rows:
            set_cursor(db, f"ads_{platform}", max(r["day"] for r in rows).isoformat())
        db.execute("commit")
    except Exception:
        db.execute("rollback")
        raise
    return len(rows)


def json_dump(x) -> str:
    import json
    return json.dumps(x, separators=(",", ":"))


def extract_all(db, http: httpx.Client, base: str, token: str, batch: str) -> dict:
    """Load every source; one failing source doesn't stop the others. Returns {source: rows | error}."""
    db.execute(RAW_DDL)
    jobs = {"shop orders": lambda: load_shop(db, http, base, token, batch),
            "stripe charges": lambda: load_stripe(db, http, base, token, "charges", batch),
            "stripe refunds": lambda: load_stripe(db, http, base, token, "refunds", batch),
            "google ads": lambda: load_ads(db, http, base, token, "google", batch),
            "meta ads": lambda: load_ads(db, http, base, token, "meta", batch)}
    out = {}
    for name, job in jobs.items():
        try:
            out[name] = {"rows": job()}
        except SourceError as e:
            out[name] = {"error": str(e)}
    return out


def now_batch() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
