#!/usr/bin/env python3
"""Find under-served Apify Store niches from the public Store API (no account needed).

Groups actors by target (derived from the actor slug), then scores each target:
demand = monthly users across all actors for it; weak leader = high failure rate or low rating.

Usage: python3 niche_finder.py fetch        # downloads store.json (polite, ~16 requests)
       python3 niche_finder.py rank         # writes niches.csv (rough first pass)
       python3 niche_finder.py count <term> # real competition for one candidate niche
       python3 niche_finder.py test         # self-check

shortcut: the Store API stops paging at ~15k actors (sorted by popularity, so the tail has <10 users);
rank groups by slug, which misses actors named differently for the same site. Always confirm with `count`.
"""
import csv, json, re, statistics, sys, time, urllib.request
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
STORE = HERE / "store.json"
API = "https://api.apify.com/v2/store?limit=1000&offset={}"
UA = "niche-finder/0.1 (personal market research)"

GENERIC = set("""scraper scrapers scraping scrape api apis extractor extract crawler crawl data actor actors
mcp server pro lite fast cheap free best advanced simple bulk all in one v1 v2 v3 tool tools
search results result details detail info full new the and for with by of to from""".split())

# Personal-data / social targets: GDPR risk and saturated. Skip.
EXCLUDE = set("""linkedin instagram facebook tiktok twitter x threads snapchat whatsapp telegram
email emails phone phones contact contacts leads lead people profile profiles followers onlyfans
tinder dating""".split())


def target_key(slug: str) -> str:
    toks = [t for t in re.split(r"[-_.]+", slug.lower()) if t and t not in GENERIC and not t.isdigit()]
    return "-".join(toks[:2])


def fetch():
    items, offset = [], 0
    while True:
        req = urllib.request.Request(API.format(offset), headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)["data"]
        page = data["items"]
        if not page:
            break
        items += page
        offset += data["limit"]
        print(f"{len(items)}/{data['total']}", file=sys.stderr)
        if offset >= data["total"]:
            break
        time.sleep(1)  # be polite
    STORE.write_text(json.dumps(items))
    print(f"saved {len(items)} actors", file=sys.stderr)


def fail_rate(stats):
    s = stats.get("publicActorRunStats30Days") or {}
    total = s.get("TOTAL") or 0
    bad = sum(s.get(k, 0) for k in ("FAILED", "TIMED-OUT", "ABORTED"))
    return bad / total if total else None


def score_groups(items):
    groups = defaultdict(list)
    for it in items:
        key = target_key(it.get("name", ""))
        if not key or set(key.split("-")) & EXCLUDE:
            continue
        groups[key].append(it)

    rows = []
    for key, acts in groups.items():
        users30 = [a["stats"].get("totalUsers30Days") or 0 for a in acts]
        demand = sum(users30)
        if demand < 50:
            continue
        leader = max(acts, key=lambda a: a["stats"].get("totalUsers30Days") or 0)
        lead_users = leader["stats"].get("totalUsers30Days") or 0
        fr = fail_rate(leader["stats"])
        rating = leader["stats"].get("actorReviewRating") or leader.get("actorReviewRating")
        active = sum(1 for u in users30 if u >= 5)
        weakness = (fr or 0) + (0.3 if rating and rating < 4 else 0)
        # Opportunity: much demand, few active competitors, leader shaky.
        score = demand / (1 + active) * (0.5 + weakness)
        rows.append({
            "target": key, "score": round(score, 1), "monthly_users": demand, "actors": len(acts),
            "active_actors": active, "leader": f"{leader['username']}/{leader['name']}",
            "leader_users30": lead_users, "leader_fail_rate": None if fr is None else round(fr, 3),
            "leader_rating": None if rating is None else round(rating, 2),
            "leader_pricing": (leader.get("currentPricingInfo") or {}).get("pricingModel"),
        })
    return sorted(rows, key=lambda r: r["score"], reverse=True)


def rank():
    rows = score_groups(json.loads(STORE.read_text()))
    with open(HERE / "niches.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    for r in rows[:40]:
        print(r)


def count(term):
    """Real competition for one candidate niche: full-text match on name/title/description."""
    term = term.lower()
    hits = [i for i in json.loads(STORE.read_text())
            if term in f"{i['name']} {i['title']} {i.get('description') or ''}".lower()]
    hits.sort(key=lambda i: -(i["stats"].get("totalUsers30Days") or 0))
    users = [i["stats"].get("totalUsers30Days") or 0 for i in hits]
    print(f"'{term}': {len(hits)} actors, {sum(users)} monthly users, {sum(u >= 5 for u in users)} with 5+ users")
    for i in hits[:10]:
        s = i["stats"]
        print(f"  {s.get('totalUsers30Days') or 0:>6} users  rating={round(s.get('actorReviewRating') or 0, 2):<4} "
              f"fail={fail_rate(s) if fail_rate(s) is None else round(fail_rate(s), 3)}  {i['username']}/{i['name']}")


def test():
    assert target_key("google-maps-scraper") == "google-maps"
    assert target_key("trendyol-scraper-v2") == "trendyol"
    assert target_key("fast-zillow-api-scraper") == "zillow"
    a = lambda u, name, fr: {"name": name, "username": "x", "stats": {"totalUsers30Days": u,
        "publicActorRunStats30Days": {"TOTAL": 100, "FAILED": int(fr * 100)}}}
    rows = score_groups([a(300, "zillow-scraper", 0.4), a(10, "zillow-api", 0), a(500, "linkedin-scraper", 0.9),
                         a(400, "redfin-scraper", 0.0), a(10, "redfin-api", 0), a(10, "redfin-x", 0)])
    keys = [r["target"] for r in rows]
    assert "linkedin" not in keys, "personal-data targets must be excluded"
    assert keys[0] == "zillow", f"shaky leader should rank first, got {keys}"
    print("ok")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "test"
    if cmd == "count":
        count(" ".join(sys.argv[2:]))
    else:
        {"fetch": fetch, "rank": rank, "test": test}[cmd]()
