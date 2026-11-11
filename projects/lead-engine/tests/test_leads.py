import json, re
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lead_engine import db as store
from lead_engine.enrich import DataAPI, process
from lead_engine.fakes import create_app as create_fakes
from lead_engine.intake import BadFile, import_csv
from lead_engine.outreach import check_token, enroll_qualified, handle_reply, in_window, push_interested, send_due, unsubscribe_token
from lead_engine.web import create_app as create_web

SAMPLES = Path(__file__).parents[1] / "samples"
ENV = {"SECRET_KEY": "s", "DATA_API_KEY": "k", "DATA_API_URL": "http://apis", "FAKES_ADMIN_TOKEN": "admin", "CRM_TOKEN": "crm",
       "CRM_URL": "http://apis", "FORM_WEBHOOK_SECRET": "form", "FROM_EMAIL": "basar@outreach.example", "SMTP_HOST": "x",
       "PUBLIC_URL": "http://leads", "DEMO_SEED": "true"}


class Outbox:
    sender = "basar@outreach.example"

    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "leads.db"))
    conn = store.connect(str(tmp_path / "leads.db"))
    apis = TestClient(create_fakes(), base_url="http://apis")
    yield conn, apis, DataAPI(http=apis)
    conn.close()


def status(conn, email):
    return conn.execute("select status from leads where email = ?", (email,)).fetchone()[0]


def load(conn, api):
    import_csv(conn, "apollo-export.csv", (SAMPLES / "apollo-export.csv").read_bytes(), "test")
    process(conn, api, limit=100)


def test_import_normalises_merges_and_reports(env):
    conn, *_ = env
    r = import_csv(conn, "apollo-export.csv", (SAMPLES / "apollo-export.csv").read_bytes(), "test")
    assert (r["format"], r["rows"], r["created"], r["merged"], r["invalid"]) == ("apollo", 20, 18, 1, 1)
    lena = conn.execute("select * from leads where email = 'lena@nordlicht-shop.de'").fetchone()
    assert lena["title"] == "Founder" and lena["country"] == "DE"  # the first source wins, the duplicate didn't overwrite
    thomas = conn.execute("select * from leads where email = 't.wolf@fabrikat-moebel.de'").fetchone()
    assert (thomas["first_name"], thomas["last_name"]) == ("Thomas", "Wolf")
    kai = conn.execute("select * from leads where email = 'kai.lorenz@gmail.com'").fetchone()
    assert kai["domain"] == "alpaka-wolle.at"  # personal address: the company comes from the website
    conn.execute("insert into suppressions values ('competitor.de', 'competitor', 'now', 'test')")
    r2 = import_csv(conn, "salesnav.csv", (SAMPLES / "salesnav-export.csv").read_bytes(), "test")
    assert (r2["format"], r2["created"], r2["merged"], r2["suppressed"]) == ("salesnav", 2, 2, 1)
    assert "linkedin.com/in/lena" in conn.execute("select linkedin from leads where email = 'lena@nordlicht-shop.de'").fetchone()[0]  # blank filled
    assert conn.execute("select count(*) from lead_sources where lead_id = ?", (lena["id"],)).fetchone()[0] == 3
    with pytest.raises(BadFile, match="No email column"):
        import_csv(conn, "x.csv", b"name,company\nA,B\n", "test")


def test_enrich_verify_and_explainable_scores(env):
    conn, apis, api = env
    load(conn, api)
    expected = {"lena@nordlicht-shop.de": ("qualified", 100), "luca@velo-atelier.ch": ("qualified", 90),
                "info@greenleaf-cosmetics.de": ("disqualified", 40), "s.collins@mega-retail-group.com": ("disqualified", 50),
                "kai.lorenz@gmail.com": ("qualified", 70), "mia@pfoten-glueck.de": ("qualified", 80)}
    for email, (st, sc) in expected.items():
        row = conn.execute("select status, score from leads where email = ?", (email,)).fetchone()
        assert (row["status"], row["score"]) == (st, sc), email
    assert "mailbox_not_found" in conn.execute("select reason from leads where email = 'm.schulz@kaffeekontor.de'").fetchone()[0]
    assert "couldn't be verified" in conn.execute("select reason from leads where email = 'tim@unknown-startup.io'").fetchone()[0] \
        or status(conn, "tim@unknown-startup.io") == "disqualified"
    detail = json.loads(conn.execute("select score_detail from leads where email = 'luca@velo-atelier.ch'").fetchone()[0])
    assert {"label": "domain accepts any address", "points": -10} in detail
    calls = apis.get("/_admin/state", headers={"x-admin": "admin"}).json()["stats"]["enrich_calls"]
    assert calls == 16  # one lookup per company domain, not per person (gmail skipped, cache reused)


def test_rate_limit_stops_the_batch_and_resumes(env):
    conn, apis, api = env
    import_csv(conn, "a.csv", (SAMPLES / "apollo-export.csv").read_bytes(), "test")
    apis.post("/_admin/chaos", json={"rate_limit": 1}, headers={"x-admin": "admin"})
    first = process(conn, api, limit=100)
    assert first["scored"] == 0 and first["wait"] == 1.0
    assert conn.execute("select count(*) from leads where status = 'new'").fetchone()[0] == 18
    process(conn, api, limit=100)
    assert conn.execute("select count(*) from leads where status = 'new'").fetchone()[0] == 0


def run_sequence(conn, api, monkeypatch):
    load(conn, api)
    monkeypatch.setenv("DAY_SECONDS_SCALE", "0")
    enroll_qualified(conn)
    box = Outbox()
    send_due(conn, box, force_window=True)
    return box


def test_sequence_threads_follow_ups_and_respects_suppression(env, monkeypatch):
    conn, _, api = env
    box = run_sequence(conn, api, monkeypatch)
    first = {m["To"].addresses[0].addr_spec: m for m in box.sent}
    assert len(first) == 11 and "Nordlicht Shop + your order flow" == first["lena@nordlicht-shop.de"]["Subject"]
    assert "runs on Shopify." in first["lena@nordlicht-shop.de"].get_content()
    assert first["lena@nordlicht-shop.de"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    conn.execute("insert into suppressions values ('mia@pfoten-glueck.de', 'manual', 'now', 'test')")  # added between steps
    box2 = Outbox()
    send_due(conn, box2, force_window=True)
    follow = {m["To"].addresses[0].addr_spec: m for m in box2.sent}
    assert "mia@pfoten-glueck.de" not in follow and len(follow) == 10
    lena = follow["lena@nordlicht-shop.de"]
    assert lena["Subject"] == "Re: Nordlicht Shop + your order flow" and lena["In-Reply-To"] == first["lena@nordlicht-shop.de"]["Message-ID"]
    send_due(conn, Outbox(), force_window=True)
    assert status(conn, "lena@nordlicht-shop.de") == "finished"
    assert "suppressed" in conn.execute("select stop_reason from enrollments e join leads l on l.id = e.lead_id where l.email = 'mia@pfoten-glueck.de'").fetchone()[0]


def test_daily_cap_and_window(env, monkeypatch):
    conn, _, api = env
    load(conn, api)
    enroll_qualified(conn)
    conn.execute("update settings set value = '4' where key = 'daily_cap'")
    box = Outbox()
    send_due(conn, box, force_window=True)
    assert len(box.sent) == 4
    s = store.settings(conn)
    assert in_window(s, datetime(2026, 11, 11, 10, 0)) and not in_window(s, datetime(2026, 11, 14, 10, 0)) and not in_window(s, datetime(2026, 11, 11, 18, 0))


def reply(to_msg, body, sender=None, subject=None, **headers):
    m = EmailMessage()
    m["From"] = sender or to_msg["To"]
    m["To"] = "basar@outreach.example"
    m["Subject"] = subject or "Re: " + to_msg["Subject"]
    m["Message-ID"] = f"<r{abs(hash(body))}@prospect.example>"
    m["In-Reply-To"] = to_msg["Message-ID"]
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    m.set_content(body + "\n\nOn Tue, Basar wrote:\n> " + to_msg.get_content().splitlines()[0])
    return m


def test_replies_are_sorted_and_stop_the_sequence(env, monkeypatch):
    conn, apis, api = env
    box = run_sequence(conn, api, monkeypatch)
    sent = {m["To"].addresses[0].addr_spec: m for m in box.sent}
    back = (datetime.now() + timedelta(days=10)).strftime("%d.%m.%Y")
    assert handle_reply(conn, reply(sent["lena@nordlicht-shop.de"], "Sounds good, can you do Tuesday 10:00?")) == "interested"
    assert handle_reply(conn, reply(sent["jonas.becker@kaffeekontor.de"], f"Ich bin bis {back} nicht im Büro.", subject="Abwesenheitsnotiz", Auto_Submitted="auto-replied")) == "auto_reply"
    assert handle_reply(conn, reply(sent["anna.gruber@bergwerk-outdoor.at"], "Bitte austragen.")) == "unsubscribe"
    assert handle_reply(conn, reply(sent["clara.fischer@greenleaf-cosmetics.de"], "Thanks, but we already have an agency for this. Not interested.")) == "not_interested"
    bounce = EmailMessage()
    bounce["From"] = "MAILER-DAEMON@mx.tulpen-design.nl"
    bounce["Subject"] = "Undelivered Mail Returned to Sender"
    bounce.set_content("Final-Recipient: rfc822; sanne@tulpen-design.nl\nStatus: 5.1.1\n")
    assert handle_reply(conn, bounce) == "bounce"
    assert handle_reply(conn, reply(sent["lena@nordlicht-shop.de"], "Sounds good, can you do Tuesday 10:00?")) is None  # same message again
    assert [status(conn, e) for e in ("lena@nordlicht-shop.de", "jonas.becker@kaffeekontor.de", "anna.gruber@bergwerk-outdoor.at",
                                       "clara.fischer@greenleaf-cosmetics.de", "sanne@tulpen-design.nl")] == \
        ["interested", "in_sequence", "unsubscribed", "not_interested", "bounced"]
    assert conn.execute("select count(*) from suppressions").fetchone()[0] == 3
    nxt = conn.execute("select next_at from enrollments e join leads l on l.id = e.lead_id where l.email = 'jonas.becker@kaffeekontor.de'").fetchone()[0]
    assert nxt > (datetime.now() + timedelta(days=10)).timestamp()  # the follow-up waits until they're back
    box2 = Outbox()
    send_due(conn, box2, force_window=True)
    got = {m["To"].addresses[0].addr_spec for m in box2.sent}
    assert not got & {"lena@nordlicht-shop.de", "jonas.becker@kaffeekontor.de", "anna.gruber@bergwerk-outdoor.at", "sanne@tulpen-design.nl"}
    # only the interested lead goes to the CRM, with its reply on the deal; an existing contact is reused
    apis.post("/crm/v3/objects/contacts", json={"properties": {"email": "lena@nordlicht-shop.de"}}, headers={"Authorization": "Bearer crm"})
    monkeypatch.setenv("CRM_URL", "http://apis")
    assert push_interested(conn, http=apis) == 1 and push_interested(conn, http=apis) == 0
    crm = apis.get("/_admin/state", headers={"x-admin": "admin"}).json()["crm"]
    assert len(crm["contacts"]) == 1 and "Tuesday 10:00" in crm["deals"][0]["properties"]["description"]
    assert crm["deals"][0]["associations"][0]["to"]["id"] == crm["contacts"][0]["id"]


def test_unsubscribe_link_and_form_webhook(env, monkeypatch):
    conn, _, api = env
    run_sequence(conn, api, monkeypatch)
    lid = conn.execute("select id from leads where email = 'luca@velo-atelier.ch'").fetchone()[0]
    token = unsubscribe_token(lid)
    assert check_token(token) == lid and check_token(f"{lid}.forged") is None and check_token(f"{lid + 1}.{token.split('.')[1]}") is None
    web = TestClient(create_web(secret="s"))
    assert web.get(f"/u/{lid}.nope").status_code == 404
    assert "unsubscribed" in web.post(f"/u/{token}").text
    assert status(conn, "luca@velo-atelier.ch") == "unsubscribed"
    import hashlib, hmac
    body = json.dumps({"email": "Neu@Lumen-Lighting.nl", "first_name": "Noor", "company": "Lumen Lighting"}).encode()
    sig = "sha256=" + hmac.new(b"form", body, hashlib.sha256).hexdigest()
    assert web.post("/webhooks/form", content=body, headers={"X-Signature": "sha256=00"}).status_code == 401
    assert web.post("/webhooks/form", content=body, headers={"X-Signature": sig}).json()["outcome"] == "created"
    assert status(conn, "neu@lumen-lighting.nl") == "new"


def test_panel(env, monkeypatch):
    conn, _, api = env
    web = TestClient(create_web(secret="s"), follow_redirects=False)
    token = re.search(r'name="csrf" value="([^"]+)"', web.get("/login").text).group(1)
    web.post("/login", data={"email": "growth@example.com", "password": "password", "csrf": token})
    token = re.search(r'name="csrf" value="([^"]+)"', web.get("/imports").text).group(1)
    r = web.post("/imports", data={"csrf": token}, files={"file": ("apollo.csv", (SAMPLES / "apollo-export.csv").read_bytes(), "text/csv")})
    assert r.status_code == 303 and "Column mapping" in web.get(r.headers["location"]).text
    process(conn, api, limit=100)
    assert "Lena" in web.get("/leads?status=qualified").text and "Funnel" in web.get("/").text
    lid = conn.execute("select id from leads where email = 'luca@velo-atelier.ch'").fetchone()[0]
    assert "domain accepts any address" in web.get(f"/leads/{lid}").text
    bad = web.post("/setup/rules", data={"csrf": token, "rules": "[{\"when\": \"x\"}]", "qualify_at": "60"})
    assert "error=" in bad.headers["location"]
    assert "msg=" in web.post("/setup/rules", data={"csrf": token, "rules": json.dumps(store.RULES), "qualify_at": "70"}).headers["location"]
    assert store.settings(conn)["qualify_at"] == 70
    assert "error=" in web.post("/setup/sequence", data={"csrf": token, "body0": "Hi {first_name"}).headers["location"]
