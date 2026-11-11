"""Enrichment, verification and scoring.

Each new lead's address is verified, its company looked up by domain (cached for 30 days, one call per company, not
per person), and it is scored against the ideal-customer rules. Every point is explained, so a salesperson can see
why a lead was qualified or not. Rate limits are honoured (Retry-After), outages stop the batch and retry next
cycle, and a lead is never qualified on missing data by accident.
"""
import json, os, time

import httpx

from .db import event, now, set_status, settings
from .intake import FREE, ROLE

CACHE_SECONDS = 30 * 24 * 3600


class Temporary(Exception):
    def __init__(self, msg: str, retry_after: float = 0):
        super().__init__(msg)
        self.retry_after = retry_after


class DataAPI:
    def __init__(self, http: httpx.Client | None = None):
        self.base = os.environ["DATA_API_URL"].rstrip("/")
        self.key = os.environ["DATA_API_KEY"]
        self.http = http or httpx.Client(timeout=15)

    def _get(self, path: str, params: dict, headers: dict) -> dict | None:
        try:
            r = self.http.get(self.base + path, params=params, headers=headers)
        except httpx.TransportError as e:
            raise Temporary(f"network error: {e}")
        if r.status_code == 429:
            raise Temporary("rate limited", float(r.headers.get("retry-after", 5)))
        if r.status_code >= 500:
            raise Temporary(f"{r.status_code} from the data provider")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def company(self, domain: str) -> dict | None:
        return self._get("/enrich/v1/companies/find", {"domain": domain}, {"Authorization": f"Bearer {self.key}"})

    def verify(self, email: str) -> dict:
        return self._get("/verify/v2/validate", {"email": email}, {"X-Api-Key": self.key})


def company(db, api: DataAPI, domain: str) -> dict | None:
    if not domain or domain in FREE:
        return None
    hit = db.execute("select data, fetched_at from enrich_cache where domain = ?", (domain,)).fetchone()
    if hit and time.time() - hit["fetched_at"] < CACHE_SECONDS:
        return json.loads(hit["data"]) if hit["data"] else None
    raw = api.company(domain)
    data = raw and {"name": raw["name"], "industry": raw.get("industry"), "employees": (raw.get("metrics") or {}).get("employees"),
                    "country": (raw.get("geo") or {}).get("countryCode"), "tech": raw.get("tech") or []}
    db.execute("insert into enrich_cache values (?, ?, ?) on conflict(domain) do update set data = excluded.data, fetched_at = excluded.fetched_at",
               (domain, json.dumps(data) if data else None, time.time()))
    return data


def email_kind(email: str, check: dict) -> list[str]:
    local, _, domain = email.partition("@")
    kinds = []
    if domain in FREE:
        kinds.append("free")
    if local in ROLE or check.get("sub_status") == "role_based":
        kinds.append("role")
    if check.get("status") == "catch-all":
        kinds.append("catch_all")
    return kinds


def score(lead: dict, rules: list[dict]) -> tuple[int, list[dict]]:
    e = lead.get("enrichment") or {}
    facts = {"industry": e.get("industry"), "tech": e.get("tech") or [], "employees": e.get("employees"),
             "country": lead.get("country") or e.get("country"), "title": (lead.get("title") or "").lower(), "email": lead.get("email_kinds") or []}
    hits = []
    for r in rules:
        v = facts.get(r["when"])
        if "in" in r:
            ok = any(t in r["in"] for t in v) if isinstance(v, list) else v in r["in"]
        elif "between" in r:
            ok = v is not None and r["between"][0] <= v <= r["between"][1]
        elif "contains" in r:
            ok = any(w in v for w in r["contains"])
        elif "is" in r:
            ok = r["is"] in v
        else:
            ok = False
        if ok:
            hits.append({"label": r["label"], "points": r["points"]})
    return sum(h["points"] for h in hits), hits


def process(db, api: DataAPI, limit: int = 25) -> dict:
    """Enrich, verify and score up to `limit` new leads. Stops at the first temporary error and says how long to wait."""
    s = settings(db)
    done = {"scored": 0, "qualified": 0, "disqualified": 0, "wait": 0.0}
    for lead in db.execute("select * from leads where status = 'new' order by id limit ?", (limit,)).fetchall():
        try:
            check = api.verify(lead["email"])
            data = company(db, api, lead["domain"])
        except Temporary as e:
            event(db, lead["id"], "enrich_retry", str(e))
            done["wait"] = max(e.retry_after, 1.0)
            break
        kinds = email_kind(lead["email"], check)
        db.execute("update leads set enrichment = ?, email_check = ?, company = coalesce(nullif(company, ''), ?), country = coalesce(nullif(country, ''), ?), updated_at = ? where id = ?",
                   (json.dumps(data) if data else None, json.dumps({**check, "kinds": kinds}), (data or {}).get("name"), (data or {}).get("country"), now(), lead["id"]))
        event(db, lead["id"], "enriched", f"email {check['status']}" + (f"; {data['name']}, {data['industry']}, {data['employees']} people" if data else "; company not found"))
        if check["status"] in ("invalid", "do_not_mail"):
            set_status(db, lead["id"], "disqualified", f"email {check['status']} ({check.get('sub_status') or 'no reason given'})")
            done["disqualified"] += 1
            continue
        total, hits = score({**dict(lead), "enrichment": data, "email_kinds": kinds, "country": lead["country"] or (data or {}).get("country")}, s["rules"])
        db.execute("update leads set score = ?, score_detail = ? where id = ?", (total, json.dumps(hits), lead["id"]))
        done["scored"] += 1
        if total >= s["qualify_at"] and check["status"] != "unknown":
            set_status(db, lead["id"], "qualified", f"score {total}")
            done["qualified"] += 1
        else:
            why = "email couldn't be verified" if check["status"] == "unknown" and total >= s["qualify_at"] else f"score {total} below {s['qualify_at']}"
            set_status(db, lead["id"], "disqualified", why)
            done["disqualified"] += 1
    return done
