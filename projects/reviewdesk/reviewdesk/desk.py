"""Review handling policy.

- 5 stars, and 4 stars without criticism: the draft is scheduled and posted after a delay (an hour by default), so a
  manager can still change it, but nobody has to.
- 4 stars with a "but", and 1-3 stars: a person approves or edits the draft first.
- 1-2 stars and anything about health, safety or legal threats: also an alert in Slack, because it needs an answer
  today. Serious claims are never discussed in public; the draft asks the reviewer to get in touch privately.
- A review answered elsewhere (in the Google app) is left alone; one edited after our reply is flagged.
"""
import json, os
from datetime import datetime, timedelta, timezone

import httpx

from .db import audit, now
from .drafting import analyse, draft, language

STARS = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}
BUT = r"\b(but|however|although|aber|leider|allerdings|jedoch)\b"


class Google:
    """Google Business Profile API: reviews with pageToken paging, and the reply endpoint."""

    def __init__(self, http=None):
        self.base = os.environ["GOOGLE_URL"].rstrip("/") + "/v4/accounts/" + os.environ.get("GOOGLE_ACCOUNT", "accounts/42").split("/")[-1]
        self.h = {"Authorization": "Bearer " + os.environ["GOOGLE_TOKEN"]}
        self.http = http or httpx.Client(timeout=20)

    def reviews(self, location: str) -> list[dict]:
        out, token = [], None
        while True:
            r = self.http.get(f"{self.base}/{location}/reviews", params={"pageSize": 50} | ({"pageToken": token} if token else {}), headers=self.h)
            r.raise_for_status()
            body = r.json()
            out += body.get("reviews", [])
            token = body.get("nextPageToken")
            if not token:
                return out

    def reply(self, location: str, review_id: str, comment: str) -> dict:
        r = self.http.put(f"{self.base}/{location}/reviews/{review_id}/reply", json={"comment": comment}, headers=self.h)
        r.raise_for_status()
        return r.json()


def later(minutes: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def alert(text: str) -> bool:
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        return False
    try:
        return httpx.post(url, json={"text": text}, timeout=10).status_code == 200
    except httpx.TransportError:
        return False


def sync(db, google: Google, delay_minutes: float | None = None) -> dict:
    import re
    delay = delay_minutes if delay_minutes is not None else float(os.environ.get("AUTO_DELAY_MINUTES", 60))
    out = {"new": 0, "scheduled": 0, "approval": 0, "alerts": 0, "changed": 0}
    for loc in db.execute("select * from locations").fetchall():
        for r in google.reviews(loc["id"]):
            stars, text = STARS[r["starRating"]], (r.get("comment") or "").strip()
            old = db.execute("select * from reviews where id = ?", (r["reviewId"],)).fetchone()
            if old and old["stars"] == stars and old["text"] == text:
                continue
            if old and old["status"] == "posted":
                audit(db, old["id"], "google", f"the review was edited after our reply: now {stars} stars")
                db.execute("update reviews set stars = ?, text = ? where id = ?", (stars, text, old["id"]))
                out["changed"] += 1
                continue
            lang = language(text) if text else "de"
            topics, urgent = analyse(text)
            d = draft(author=r["reviewer"]["displayName"], stars=stars, text=text, lang=lang, topics=topics, urgent=urgent,
                      location=loc["name"], email=loc["contact_email"])
            if r.get("reviewReply"):
                status = "replied_elsewhere"
            elif urgent or stars <= 3 or (stars == 4 and re.search(BUT, text, re.I)):
                status = "awaiting_approval"
            else:
                status = "scheduled"
            db.execute("""insert into reviews (id, location_id, author, stars, text, lang, topics, urgent, created, fetched_at, status, draft, post_after)
                          values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) on conflict(id) do update set stars = excluded.stars, text = excluded.text,
                          lang = excluded.lang, topics = excluded.topics, urgent = excluded.urgent, status = excluded.status, draft = excluded.draft,
                          post_after = excluded.post_after""",
                       (r["reviewId"], loc["id"], r["reviewer"]["displayName"], stars, text, lang, json.dumps(topics), urgent, r["createTime"], now(), status, d,
                        later(delay) if status == "scheduled" else None))
            audit(db, r["reviewId"], "sync", f"{'updated' if old else 'new'} {stars}★ review: {status.replace('_', ' ')}" + (f" ({urgent})" if urgent else ""))
            out["new"] += not old
            out["changed"] += bool(old)
            out["scheduled" if status == "scheduled" else "approval"] += status in ("scheduled", "awaiting_approval")
            if status == "awaiting_approval" and (urgent or stars <= 2):
                why = f"{urgent} issue" if urgent else f"{stars}-star review"
                if alert(f":rotating_light: {why} at {loc['name']} from {r['reviewer']['displayName']}: \"{text[:200]}\" Reply drafted, waiting for approval."):
                    out["alerts"] += 1
        db.execute("update locations set last_sync = ? where id = ?", (now(), loc["id"]))
    return out


def post(db, google: Google, rid: str, text: str, who: str) -> bool:
    r = db.execute("select * from reviews where id = ?", (rid,)).fetchone()
    try:
        google.reply(r["location_id"], rid, text)
    except httpx.HTTPError as e:
        db.execute("update reviews set reply_error = ? where id = ?", (str(e)[:200], rid))
        return False
    db.execute("update reviews set status = 'posted', final = ?, decided_by = ?, posted_at = ?, reply_error = null where id = ?", (text, who, now(), rid))
    audit(db, rid, who, "reply posted" + ("" if text == r["draft"] else " (edited)"))
    return True


def post_due(db, google: Google) -> int:
    n = 0
    for r in db.execute("select * from reviews where status = 'scheduled' and post_after <= ?", (now(),)).fetchall():
        n += post(db, google, r["id"], r["draft"], "auto (after the review window)")
    return n


def stats(db) -> list[dict]:
    out = []
    for loc in db.execute("select * from locations").fetchall():
        rows = db.execute("select * from reviews where location_id = ?", (loc["id"],)).fetchall()
        posted = [r for r in rows if r["status"] in ("posted", "replied_elsewhere")]
        times = [(datetime.strptime(r["posted_at"], "%Y-%m-%d %H:%M:%S") - datetime.strptime(r["fetched_at"], "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600
                 for r in rows if r["posted_at"]]
        topics = {}
        for r in rows:
            if r["stars"] <= 3:
                for t in json.loads(r["topics"]):
                    topics[t] = topics.get(t, 0) + 1
        out.append({"loc": loc, "count": len(rows), "avg": round(sum(r["stars"] for r in rows) / len(rows), 2) if rows else None,
                    "replied": round(100 * len(posted) / len(rows)) if rows else 0, "hours": round(sum(times) / len(times), 1) if times else None,
                    "complaints": sorted(topics.items(), key=lambda kv: -kv[1])})
    return out
