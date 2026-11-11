"""Demo stand-ins with the real request shapes: the Google Business Profile reviews API (pageToken paging, reply PUT)
and a Slack incoming webhook. /_admin endpoints (token-protected) let customers write and edit reviews."""
import os, secrets, time
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Header, HTTPException, Request

WORDS = {1: "ONE", 2: "TWO", 3: "THREE", 4: "FOUR", 5: "FIVE"}
NUM = {v: k for k, v in WORDS.items()}
SEED = [
    ("locations/1001", "Jonas Keller", 5, "Best ramen in Karlsruhe! The broth is incredible and the staff were so friendly."),
    ("locations/1001", "Anna Schmidt", 5, "Super lecker und sehr freundliche Bedienung. Wir kommen wieder!"),
    ("locations/1001", "Tom R.", 4, "Great food, but we waited 40 minutes for a table even though we had a reservation."),
    ("locations/1001", "Lukas Weber", 2, "Leider war die Suppe versalzen und wir haben ewig auf das Essen gewartet. Für den Preis enttäuschend."),
    ("locations/1001", "Google User", 5, ""),
    ("locations/1002", "Sophie M.", 1, "I got food poisoning after eating here on Saturday. Never again."),
    ("locations/1002", "Mehmet Yilmaz", 3, "The noodles were okay but the toilet was dirty and the waiter was rude."),
    ("locations/1002", "Clara Fischer", 5, "Wonderful evening, delicious food!"),
]


def iso(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)
    token, admin_token = os.environ["GOOGLE_TOKEN"], os.environ["FAKES_ADMIN_TOKEN"]
    reviews: dict[str, dict] = {}
    slack: list[dict] = []
    for i, (loc, who, stars, text) in enumerate(SEED):
        rid = f"r{i + 1:03d}"
        reviews[rid] = {"location": loc, "reviewId": rid, "reviewer": {"displayName": who}, "starRating": WORDS[stars], "comment": text,
                        "createTime": iso(600 - i * 30), "updateTime": iso(600 - i * 30)}

    def auth(h: str):
        if not secrets.compare_digest(h.removeprefix("Bearer "), token):
            raise HTTPException(401, {"error": {"code": 401, "status": "UNAUTHENTICATED"}})

    def admin(x):
        if not secrets.compare_digest(x, admin_token):
            raise HTTPException(403)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/google/v4/accounts/{acc}/locations/{loc}/reviews")
    def list_reviews(acc: str, loc: str, pageSize: int = 50, pageToken: str = "", authorization: str = Header("")):
        auth(authorization)
        rows = sorted((r for r in reviews.values() if r["location"] == f"locations/{loc}"), key=lambda r: r["updateTime"], reverse=True)
        start = int(pageToken or 0)
        page = rows[start:start + min(pageSize, 3)]  # small pages so paging is exercised
        body = {"reviews": [{k: v for k, v in r.items() if k != "location"} for r in page], "totalReviewCount": len(rows),
                "averageRating": round(sum(NUM[r["starRating"]] for r in rows) / len(rows), 1) if rows else 0}
        if start + len(page) < len(rows):
            body["nextPageToken"] = str(start + len(page))
        return body

    @app.put("/google/v4/accounts/{acc}/locations/{loc}/reviews/{rid}/reply")
    def reply(acc: str, loc: str, rid: str, body: dict, authorization: str = Header("")):
        auth(authorization)
        if rid not in reviews:
            raise HTTPException(404)
        reviews[rid]["reviewReply"] = {"comment": body["comment"], "updateTime": iso(0)}
        return reviews[rid]["reviewReply"]

    @app.post("/slack/hook")
    async def slack_hook(request: Request):
        slack.append(await request.json())
        return "ok"

    @app.post("/_admin/review")
    def add(body: dict, x_admin: str = Header("")):
        admin(x_admin)
        rid = body.get("id") or f"r{len(reviews) + 1:03d}"
        old = reviews.get(rid, {})
        reviews[rid] = {**old, "location": body.get("location", old.get("location", "locations/1001")), "reviewId": rid,
                        "reviewer": {"displayName": body.get("author", old.get("reviewer", {}).get("displayName", "Guest"))},
                        "starRating": WORDS[body["stars"]], "comment": body.get("text", ""), "createTime": old.get("createTime", iso(0)), "updateTime": iso(0)}
        if body.get("owner_reply"):
            reviews[rid]["reviewReply"] = {"comment": body["owner_reply"], "updateTime": iso(0)}
        return reviews[rid]

    @app.get("/_admin/state")
    def state(x_admin: str = Header("")):
        admin(x_admin)
        return {"reviews": reviews, "slack": slack}

    return app
