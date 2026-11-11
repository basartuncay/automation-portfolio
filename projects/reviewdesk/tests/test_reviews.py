import re

import pytest
from fastapi.testclient import TestClient

from reviewdesk import db as store, desk
from reviewdesk.drafting import analyse, draft, first_name, language
from reviewdesk.fakes import create_app as create_fakes
from reviewdesk.web import create_app as create_web

ENV = {"SECRET_KEY": "s", "GOOGLE_TOKEN": "g", "FAKES_ADMIN_TOKEN": "admin", "GOOGLE_URL": "http://apis/google", "SLACK_WEBHOOK_URL": "", "DEMO_SEED": "true"}
A = {"x-admin": "admin"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "r.db"))
    conn = store.connect(str(tmp_path / "r.db"))
    apis = TestClient(create_fakes(), base_url="http://apis")
    yield conn, apis, desk.Google(http=apis)
    conn.close()


def row(conn, rid):
    return conn.execute("select * from reviews where id = ?", (rid,)).fetchone()


def test_language_topics_urgent_and_names():
    assert language("Super lecker und sehr freundlich") == "de" and language("Great food, friendly staff") == "en"
    assert analyse("We waited ages and the waiter was rude")[0] == ["service", "waiting"]
    assert analyse("Ich hatte danach eine Lebensmittelvergiftung")[1] == "health" and analyse("My lawyer will hear about this")[1] == "legal"
    assert first_name("Google User") == "" and first_name("Tom R.") == "Tom" and first_name("") == ""


def test_drafts_fit_the_review():
    d = draft(author="Lukas Weber", stars=2, text="", lang="de", topics=["food", "waiting", "price"], urgent=None, location="Kiku Noodle Bar, Kaiserstraße", email="hello@kiku.example")
    assert d.startswith("Hallo Lukas,") and "Küche" in d and "Wartezeiten" in d and "hello@kiku.example" in d and "Mara, Kiku Noodle Bar" in d
    serious = draft(author="Sophie M.", stars=1, text="food poisoning", lang="en", topics=["food"], urgent="health", location="X", email="e@x")
    assert "poisoning" not in serious and "contact me directly" in serious and "kitchen" not in serious  # the claim isn't discussed in public
    happy = draft(author="Google User", stars=5, text="", lang="en", topics=[], urgent=None, location="X", email="e@x")
    assert happy.startswith("Hi,\n") and "lovely review" in happy


def test_policy_sync_paging_and_alerts(env, monkeypatch):
    conn, apis, google = env
    sent = []
    monkeypatch.setattr(desk, "alert", lambda text: sent.append(text) or True)
    r = desk.sync(conn, google, delay_minutes=60)
    assert r["new"] == 8  # both locations, across pages of 3
    status = {x["id"]: x["status"] for x in conn.execute("select id, status from reviews")}
    assert [status[f"r00{i}"] for i in range(1, 9)] == ["scheduled", "scheduled", "awaiting_approval", "awaiting_approval", "scheduled",
                                                       "awaiting_approval", "awaiting_approval", "scheduled"]
    assert row(conn, "r006")["urgent"] == "health" and len(sent) == 2  # the 1-star food poisoning and the 2-star
    assert desk.sync(conn, google)["new"] == 0  # nothing changed


def test_scheduled_posts_after_the_window_and_approval_posts_now(env):
    conn, apis, google = env
    desk.sync(conn, google, delay_minutes=60)
    assert desk.post_due(conn, google) == 0  # still inside the review window
    conn.execute("update reviews set post_after = '2000-01-01 00:00:00' where status = 'scheduled'")
    assert desk.post_due(conn, google) == 4
    reviews = apis.get("/_admin/state", headers=A).json()["reviews"]
    assert reviews["r001"]["reviewReply"]["comment"].startswith("Hi Jonas,") and "reviewReply" not in reviews["r004"]
    assert desk.post(conn, google, "r004", "Hallo Lukas, danke! (edited)", "Mara")
    assert row(conn, "r004")["status"] == "posted" and reviews != apis.get("/_admin/state", headers=A).json()["reviews"]


def test_answered_elsewhere_and_edited_reviews(env):
    conn, apis, google = env
    desk.sync(conn, google)
    apis.post("/_admin/review", json={"author": "Eva", "stars": 5, "text": "Lovely!", "owner_reply": "Thanks Eva!"}, headers=A)
    desk.sync(conn, google)
    assert row(conn, "r009")["status"] == "replied_elsewhere"
    desk.post(conn, google, "r001", "Thanks Jonas!", "Mara")
    apis.post("/_admin/review", json={"id": "r001", "stars": 3, "text": "Changed my mind after the second visit, the broth was cold."}, headers=A)
    assert desk.sync(conn, google)["changed"] == 1
    assert "edited after our reply" in conn.execute("select what from audit where review_id = 'r001' order by id desc").fetchone()[0]
    apis.post("/_admin/review", json={"id": "r003", "stars": 2, "text": "Actually the reservation was lost and we waited 40 minutes."}, headers=A)
    desk.sync(conn, google)
    assert row(conn, "r003")["stars"] == 2 and "booking" in row(conn, "r003")["topics"]  # not posted yet: re-read and re-drafted


def test_panel(env):
    conn, apis, google = env
    web = TestClient(create_web(secret="s", google=google), follow_redirects=False)
    token = re.search(r'name="csrf" value="([^"]+)"', web.get("/login").text).group(1)
    web.post("/login", data={"email": "manager@example.com", "password": "password", "csrf": token})
    token = re.search(r'name="csrf" value="([^"]+)"', web.get("/").text).group(1)
    assert "8+new" in web.post("/sync", data={"csrf": token}).headers["location"]
    page = web.get("/").text
    assert page.index('data-review="r006"') < page.index('data-review="r004"')  # the urgent one first
    web.post("/reviews/r007", data={"csrf": token, "action": "post", "text": "Hi Mehmet, sorry about that. (edited by Mara)"})
    assert apis.get("/_admin/state", headers=A).json()["reviews"]["r007"]["reviewReply"]["comment"].endswith("(edited by Mara)")
    web.post("/reviews/r004", data={"csrf": token, "action": "skip"})
    assert row(conn, "r004")["status"] == "skipped"
    assert "Complaints" in web.get("/stats").text
