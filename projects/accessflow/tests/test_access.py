import hashlib, hmac, json, re
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from accessflow import db as store, engine, hris
from accessflow.fakes import create_app as create_fakes, people
from accessflow.targets import GitHub, Google, Slack
from accessflow.web import approval_token, create_app as create_web

ENV = {"SECRET_KEY": "s", "FAKES_ADMIN_TOKEN": "admin", "HRIS_WEBHOOK_URL": "http://unreachable.invalid/webhooks/hris", "HRIS_WEBHOOK_SECRET": "hook",
       "HRIS_TOKEN": "h", "GOOGLE_TOKEN": "g", "SLACK_TOKEN": "s", "GITHUB_TOKEN": "gh", "HRIS_URL": "http://apis/hris", "GOOGLE_URL": "http://apis/google",
       "SLACK_URL": "http://apis/slack", "GITHUB_URL": "http://apis/github", "PUBLIC_URL": "http://af", "DEMO_SEED": "true"}
A = {"x-admin": "admin"}
d = lambda n: (date.today() + timedelta(days=n)).isoformat()


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "a.db"))
    conn = store.connect(str(tmp_path / "a.db"))
    apis = TestClient(create_fakes(), base_url="http://apis")
    clients = {"google": Google(http=apis), "slack": Slack(http=apis), "github": GitHub(http=apis)}
    for p in people():
        hris.apply(conn, p, "test")
    engine.run(conn, clients)
    yield conn, apis, clients
    conn.close()


def state(apis):
    return apis.get("/_admin/state", headers=A).json()


def settle(conn, clients, rounds=4, today=None):
    for _ in range(rounds):
        engine.plan_all(conn, today)
        conn.execute("update actions set next_at = 0 where status = 'pending'")
        engine.run(conn, clients)


def test_existing_staff_get_their_department_access(env):
    conn, apis, _ = env
    st = state(apis)
    assert set(st["google"]["users"]) == {f"{p['email']}" for p in people()}
    assert st["google"]["groups"]["eng@company.example"] == ["hanna.berg@company.example", "jonas.klein@company.example"]
    assert st["github"]["members"] == ["hberg-dev", "jklein"] and st["github"]["teams"]["engineering"] == ["hberg-dev", "jklein"]
    assert sorted(st["slack"]["groups"]["sales"]) and conn.execute("select count(*) from actions where status != 'done'").fetchone()[0] == 0


def test_joiner_only_from_the_day_before_the_start(env):
    conn, apis, clients = env
    new = {"id": "E010", "name": "Mira Stein", "email": "mira.stein@company.example", "personal_email": "mira@mail.example", "github": "mstein",
           "department": "Engineering", "title": "Engineer", "manager_id": "E001", "start_date": d(10), "end_date": None}
    assert hris.apply(conn, new, "test") == []  # starts in 10 days: nothing yet
    assert engine.plan(conn, "E010", today=date.today() + timedelta(days=9))  # the day before: everything planned
    settle(conn, clients, today=date.today() + timedelta(days=9))
    st = state(apis)
    assert "mira.stein@company.example" in st["google"]["users"] and "mstein" in st["github"]["teams"]["engineering"]


def test_mover_swaps_department_access(env):
    conn, apis, clients = env
    p = {**next(x for x in people() if x["id"] == "E004"), "department": "Sales"}
    queued = hris.apply(conn, p, "test")
    assert "google add group:sales@company.example" in queued and "google remove group:finance@company.example" in queued
    settle(conn, clients)
    st = state(apis)
    assert "tom.richter@company.example" in st["google"]["groups"]["sales@company.example"]
    assert "tom.richter@company.example" not in st["google"]["groups"]["finance@company.example"]
    assert "department: 'Finance' → 'Sales'" in conn.execute("select what from audit where employee_id = 'E004' and who = 'test' order by id desc").fetchone()[0]


def test_leaver_everything_removed_drive_to_manager(env):
    conn, apis, clients = env
    p = {**next(x for x in people() if x["id"] == "E002"), "end_date": d(0), "immediate": True}
    queued = hris.apply(conn, p, "test")
    assert queued[:2] == ["google remove group:all@company.example", "google remove group:eng@company.example"] or "google transfer_drive account" in queued
    settle(conn, clients)
    st = state(apis)
    assert st["google"]["users"]["jonas.klein@company.example"]["suspended"] is True
    assert st["google"]["transfers"][0]["newOwnerUserId"] == "hanna.berg@company.example" and "jonas.klein@company.example" in st["google"]["signouts"]
    assert "jklein" not in st["github"]["members"]
    assert not next(u for u in st["slack"]["users"] if u["userName"] == "jonas.klein@company.example")["active"]
    assert conn.execute("select count(*) from grants where employee_id = 'E002' and state = 'granted'").fetchone()[0] == 0


def test_last_day_still_has_access_until_it_ends(env):
    conn, apis, clients = env
    p = {**next(x for x in people() if x["id"] == "E003"), "end_date": d(0)}
    assert hris.apply(conn, p, "test") == []  # working today
    assert engine.plan(conn, "E003", today=date.today() + timedelta(days=1))  # the next day: removed


def test_outage_is_retried_and_order_is_kept(env):
    conn, apis, clients = env
    apis.post("/_admin/chaos", json={"slack_fail": 2}, headers=A)
    hris.apply(conn, {"id": "E011", "name": "Ola Nordmann", "email": "ola@company.example", "personal_email": None, "github": None,
                      "department": "Sales", "title": "SDR", "manager_id": "E005", "start_date": d(0), "end_date": None}, "test")
    engine.run(conn, clients)
    slack = conn.execute("select op, status, attempts from actions where employee_id = 'E011' and system = 'slack' order by id").fetchall()
    assert slack[0]["status"] == "pending" and slack[0]["attempts"] == 1 and all(a["attempts"] == 0 for a in slack[1:])
    settle(conn, clients)
    assert "ola@company.example" in [u["userName"] for u in state(apis)["slack"]["users"]]


def test_privileged_access_needs_the_manager(env):
    conn, apis, clients = env
    engine.request_privileged(conn, "E002", "github", "team:admins", "Ivy IT")
    gid = conn.execute("select id from grants where resource = 'team:admins'").fetchone()[0]
    settle(conn, clients)
    assert "jklein" not in state(apis)["github"]["teams"]["admins"]
    with pytest.raises(PermissionError):
        engine.decide(conn, gid, "it@example.com", True)
    engine.decide(conn, gid, "hanna.berg@company.example", True)
    settle(conn, clients)
    assert "jklein" in state(apis)["github"]["teams"]["admins"]
    with pytest.raises(ValueError):
        engine.request_privileged(conn, "E002", "slack", "group:eng", "Ivy IT")


def test_drift_finds_orphans_and_unapproved_admins(env):
    conn, apis, clients = env
    apis.post("/_admin/manual", json={"what": "github_add", "login": "old-contractor", "team": "admins"}, headers=A)
    apis.post("/_admin/manual", json={"what": "slack_add", "email": "freelancer@gmail.com"}, headers=A)
    apis.post("/_admin/manual", json={"what": "github_add", "login": "deploy-bot"}, headers=A)
    found = {(f["system"], f["account"], f["detail"][:20]) for f in engine.scan(conn, clients)}
    assert ("github", "old-contractor", "active account with ") in found and ("github", "old-contractor", "in the admins team w") in found
    assert ("slack", "freelancer@gmail.com", "active account with ") in found and not any(a == "deploy-bot" for _, a, _ in found)
    clients["github"].remove("old-contractor")
    engine.scan(conn, clients)
    assert conn.execute("select count(*) from drift where account = 'old-contractor' and status = 'open'").fetchone()[0] == 0


def test_webhook_approval_page_and_panel(env):
    conn, apis, clients = env
    web = TestClient(create_web(secret="s", clients=clients), follow_redirects=False)
    body = json.dumps({"event": "employee.updated", "id": "w1", "employee": {**next(x for x in people() if x["id"] == "E004"), "department": "Sales"}}).encode()
    assert web.post("/webhooks/hris", content=body, headers={"X-HRIS-Signature": "00"}).status_code == 401
    sig = hmac.new(b"hook", body, hashlib.sha256).hexdigest()
    assert web.post("/webhooks/hris", content=body, headers={"X-HRIS-Signature": sig}).json()["planned"]
    assert web.post("/webhooks/hris", content=body, headers={"X-HRIS-Signature": sig}).json() == {"duplicate": True}
    token = re.search(r'name="csrf" value="([^"]+)"', web.get("/login").text).group(1)
    web.post("/login", data={"email": "it@example.com", "password": "password", "csrf": token})
    page = web.get("/people/E002").text
    token = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    web.post("/people/E002/request", data={"csrf": token, "access": "github team:admins"})
    dm = state(apis)["slack"]["dms"][-1]["text"]
    link = re.search(r"http://af(/approve/\S+)", dm).group(1)
    gid = int(link.split("/")[-1].split(".")[0])
    assert web.get(f"/approve/{gid}.forged").status_code == 404
    assert "Approved" in web.post(link, data={"decision": "approve"}).text
    assert "already decided" in web.post(link, data={"decision": "deny"}).text
    csv_ = web.get("/audit.csv").text
    assert "approved github team:admins" in csv_ and "hanna.berg@company.example" in csv_
    for p in ("/", "/actions", "/drift", "/rules"):
        assert web.get(p).status_code == 200

