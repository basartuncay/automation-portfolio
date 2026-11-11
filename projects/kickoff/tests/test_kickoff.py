import json, re, time

import pytest
from fastapi.testclient import TestClient

from kickoff import db as store, flow
from kickoff.fakes import create_app as create_fakes, stripe_header
from kickoff.services import Projects, Storage, Stripe, verify_stripe
from kickoff.web import create_app as create_web

ENV = {"SECRET_KEY": "s", "STRIPE_SECRET_KEY": "sk_test_x", "STRIPE_WEBHOOK_SECRET": "whsec_x", "STRIPE_URL": "http://apis/stripe",
       "STRIPE_WEBHOOK_URL": "http://kick/webhooks/stripe", "FAKES_ADMIN_TOKEN": "admin", "PROJECTS_URL": "http://apis/projects", "PROJECTS_TOKEN": "p",
       "STORAGE_URL": "http://apis/storage", "STORAGE_TOKEN": "st", "PUBLIC_URL": "http://kick", "DEMO_SEED": "true", "BACKOFF_SCALE": "0"}
BRIEF = "Shopify orders should land in our ERP (weclapp) and stock should flow back. Today we copy them by hand every morning."


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "k.db"))
    monkeypatch.setenv("CONTRACTS_DIR", str(tmp_path / "contracts"))
    conn = store.connect(str(tmp_path / "k.db"))
    apis = TestClient(create_fakes(), base_url="http://apis")
    yield conn, apis, tmp_path
    conn.close()


def new_client(conn, **kw):
    return flow.enquire(conn, name=kw.get("name", "Lena Hoffmann"), email_="lena@nordlicht-shop.de", company="Nordlicht Shop", brief=BRIEF,
                        package_id=1, options=kw.get("options", ["monitoring", "rush", "hacked"]))


def proposal(conn, cid):
    return conn.execute("select * from proposals where client_id = ? order by id desc limit 1", (cid,)).fetchone()


def sha_of(conn, cid):
    import hashlib
    p = proposal(conn, cid)
    c = conn.execute("select * from clients where id = ?", (cid,)).fetchone()
    pkg = conn.execute("select * from packages where id = ?", (c["package_id"],)).fetchone()
    return hashlib.sha256(flow.contract_text(p, c, pkg).encode()).hexdigest()


def outbox(conn, cid):
    return [(r["kind"], r["status"]) for r in conn.execute("select kind, status from outbox where client_id = ? order by id", (cid,))]


def test_enquiry_builds_a_draft_from_the_package(env):
    conn, *_ = env
    cid = new_client(conn)
    p = proposal(conn, cid)
    assert [i["name"] for i in json.loads(p["items"])] == ["Store integration", "Monitoring & alerts for 3 months", "Rush delivery (half the time)"]
    assert p["total_cents"] == 320000 + 45000 + 60000 and p["status"] == "draft"  # unknown option ignored
    with pytest.raises(flow.Invalid, match="valid email"):
        flow.enquire(conn, name="X", email_="nope", company="", brief=BRIEF, package_id=1, options=[])
    with pytest.raises(flow.Invalid, match="a little more"):
        flow.enquire(conn, name="X", email_="x@y.de", company="", brief="hi", package_id=1, options=[])


def test_reminders_follow_the_journey(env):
    conn, apis, tmp = env
    cid = new_client(conn)
    flow.send_proposal(conn, proposal(conn, cid)["id"], "test")
    assert outbox(conn, cid)[-2:] == [("proposal", "queued"), ("not_viewed", "queued")]
    flow.view(conn, proposal(conn, cid)["token"])
    assert ("not_viewed", "cancelled") in outbox(conn, cid) and ("not_signed", "queued") in outbox(conn, cid)
    flow.sign(conn, proposal(conn, cid)["token"], typed_name="Lena Hoffmann", agreed=True, shown_sha256=sha_of(conn, cid), ip="1.2.3.4",
              user_agent="test", stripe=Stripe(http=apis), contracts_dir=tmp / "c")
    kinds = dict(outbox(conn, cid))
    assert kinds["not_signed"] == "cancelled" and kinds["signed_copy"] == "queued" and kinds["not_paid"] == "queued"


def test_signing_checks_the_exact_document(env):
    conn, apis, tmp = env
    cid = new_client(conn)
    pid = proposal(conn, cid)["id"]
    token = proposal(conn, cid)["token"]
    assert flow.view(conn, token) == (None, None)  # drafts aren't visible to the client
    flow.send_proposal(conn, pid, "test")
    shown = sha_of(conn, cid)
    kw = dict(ip="1.2.3.4", user_agent="Firefox", stripe=Stripe(http=apis), contracts_dir=tmp / "c")
    with pytest.raises(flow.Invalid, match="changed while you had it open"):
        flow.sign(conn, token, typed_name="Lena Hoffmann", agreed=True, shown_sha256="0" * 64, **kw)
    with pytest.raises(flow.Invalid, match="tick the box"):
        flow.sign(conn, token, typed_name="Lena Hoffmann", agreed=False, shown_sha256=shown, **kw)
    r = flow.sign(conn, token, typed_name="Lena Hoffmann", agreed=True, shown_sha256=shown, **kw)
    p = proposal(conn, cid)
    sig = json.loads(p["signature"])
    assert p["status"] == "signed" and sig["sha256"] == shown == r["sha256"] and sig["ip"] == "1.2.3.4"
    pdf = open(p["pdf"], "rb").read()
    assert pdf.startswith(b"%PDF") and len(pdf) > 1500
    with pytest.raises(flow.Invalid, match="already signed"):
        flow.sign(conn, token, typed_name="Lena Hoffmann", agreed=True, shown_sha256=shown, **kw)
    session = apis.get("/_admin/state", headers={"x-admin": "admin"}).json()["sessions"][0]
    assert session["amount_total"] == 425000 * 30 // 100 and r["pay_url"] == session["url"]
    assert flow.start_payment(conn, p["id"], Stripe(http=apis)) == session["url"]  # same session, not a second charge


def test_withdraw_makes_a_new_version_and_kills_the_old_link(env):
    conn, *_ = env
    cid = new_client(conn)
    old = proposal(conn, cid)
    flow.send_proposal(conn, old["id"], "test")
    new_id = flow.withdraw(conn, old["id"], "test")
    assert flow.view(conn, old["token"]) == (None, None)
    flow.edit_proposal(conn, new_id, [{"name": "Store integration", "detail": "", "cents": 300000}], 50, "Revised after our call")
    p = proposal(conn, cid)
    assert (p["version"], p["total_cents"], p["deposit_pct"]) == (2, 300000, 50)
    with pytest.raises(flow.Invalid, match="negative"):
        flow.edit_proposal(conn, new_id, [{"name": "x", "detail": "", "cents": -5}], 30, "")


def test_expired_proposals_cant_be_signed(env):
    conn, apis, tmp = env
    cid = new_client(conn)
    flow.send_proposal(conn, proposal(conn, cid)["id"], "test")
    conn.execute("update proposals set valid_until = '2020-01-01'")
    with pytest.raises(flow.Invalid, match="expired"):
        flow.sign(conn, proposal(conn, cid)["token"], typed_name="Lena", agreed=True, shown_sha256="x", ip="", user_agent="", stripe=None, contracts_dir=tmp)


def test_stripe_signatures(env):
    body = b'{"id": "evt_1"}'
    assert verify_stripe(body, stripe_header("whsec_x", body), "whsec_x")
    assert not verify_stripe(body, stripe_header("other", body), "whsec_x")
    assert not verify_stripe(body, stripe_header("whsec_x", body, int(time.time()) - 3600), "whsec_x")  # replayed
    assert not verify_stripe(body + b" ", stripe_header("whsec_x", body), "whsec_x")


def paid_client(conn, apis, tmp):
    cid = new_client(conn)
    flow.send_proposal(conn, proposal(conn, cid)["id"], "test")
    flow.sign(conn, proposal(conn, cid)["token"], typed_name="Lena Hoffmann", agreed=True, shown_sha256=sha_of(conn, cid), ip="", user_agent="",
              stripe=Stripe(http=apis), contracts_dir=tmp / "c")
    s = apis.get("/_admin/state", headers={"x-admin": "admin"}).json()["sessions"][0]
    ev = {"id": "evt_1", "type": "checkout.session.completed", "data": {"object": {**s, "payment_status": "paid"}}}
    return cid, ev


def test_payment_then_provisioning_with_retries(env):
    conn, apis, tmp = env
    cid, ev = paid_client(conn, apis, tmp)
    bad = {**ev, "id": "evt_0", "data": {"object": {**ev["data"]["object"], "amount_total": 1}}}
    assert flow.handle_stripe_event(conn, bad) == "unknown session"  # an amount that doesn't match is not a payment
    assert flow.handle_stripe_event(conn, ev) == "paid"
    assert flow.handle_stripe_event(conn, ev) == "duplicate"
    assert dict(outbox(conn, cid))["not_paid"] == "cancelled"
    apis.post("/_admin/chaos", json={"projects_fail": 1}, headers={"x-admin": "admin"})
    projects, storage = Projects(http=apis), Storage(http=apis)
    assert flow.provision(conn, projects, storage) == 0
    assert conn.execute("select attempts, status from steps where name = 'create_project'").fetchone()[:] == (1, "pending")
    assert flow.provision(conn, projects, storage) == 3
    assert conn.execute("select stage from clients where id = ?", (cid,)).fetchone()[0] == "onboarded"
    st = apis.get("/_admin/state", headers={"x-admin": "admin"}).json()
    assert len(st["projects"]) == 1 and len(st["tasks"]["prj_1"]) == 5 and st["permissions"]["fld_1"][0]["emailAddress"] == "lena@nordlicht-shop.de"
    welcome = conn.execute("select body from outbox where kind = 'welcome'").fetchone()[0]
    assert "projects.example.com/p/1" in welcome and "drive.example.com/folders/1" in welcome
    # a crash after the project was created: the retry finds it instead of making a second one
    conn.execute("update steps set status = 'pending', next_at = 0")
    conn.execute("update clients set stage = 'paid'")
    flow.provision(conn, projects, storage)
    st = apis.get("/_admin/state", headers={"x-admin": "admin"}).json()
    assert len(st["projects"]) == 1 and len(st["tasks"]["prj_1"]) == 5 and len(st["permissions"]["fld_1"]) == 1


def test_failed_step_stops_later_steps(env, monkeypatch):
    conn, apis, tmp = env
    cid, ev = paid_client(conn, apis, tmp)
    flow.handle_stripe_event(conn, ev)
    apis.post("/_admin/chaos", json={"projects_fail": 10}, headers={"x-admin": "admin"})
    for _ in range(5):
        flow.provision(conn, Projects(http=apis), Storage(http=apis), max_attempts=3)
    assert [tuple(r) for r in conn.execute("select name, status from steps order by position")] == \
        [("create_project", "failed"), ("share_folder", "pending"), ("welcome_email", "pending")]
    assert conn.execute("select count(*) from outbox where kind = 'welcome'").fetchone()[0] == 0


def test_public_pages_and_panel(env):
    conn, apis, tmp = env
    web = TestClient(create_web(secret="s", stripe=Stripe(http=apis)), follow_redirects=False, base_url="http://kick")
    token = re.search(r'name="csrf" value="([^"]+)"', web.get("/").text).group(1)
    r = web.post("/enquire", data={"csrf": token, "name": "Lena Hoffmann", "email": "lena@nordlicht-shop.de", "company": "Nordlicht Shop",
                                   "brief": BRIEF, "package": "1", "options": ["monitoring"]})
    assert "on my desk" in r.text
    web.post("/login", data={"email": "studio@example.com", "password": "password", "csrf": token})
    board = web.get("/board").text
    token = re.search(r'name="csrf" value="([^"]+)"', board).group(1)
    assert "Nordlicht Shop" in board
    cid = conn.execute("select id from clients").fetchone()[0]
    r = web.post(f"/clients/{cid}/proposal", data={"csrf": token, "intro": "Hi", "deposit_pct": "40",
                                                   "item_name": ["Store integration", "Workshop", ""], "item_detail": ["", "", ""], "item_amount": ["3000", "450,50", ""]})
    assert "msg=" in r.headers["location"] and proposal(conn, cid)["total_cents"] == 345050
    web.post(f"/clients/{cid}/send", data={"csrf": token})
    p = proposal(conn, cid)
    page = web.get(f"/p/{p['token']}").text
    sha = re.search(r'name="sha256" value="(\w+)"', page).group(1)
    r = web.post(f"/p/{p['token']}/sign", data={"csrf": token, "typed_name": "Lena Hoffmann", "agree": "1", "sha256": sha})
    assert r.headers["location"].startswith("http://127.0.0.1:8105/stripe/pay/")
    assert web.get(f"/p/{p['token']}/pdf").headers["content-type"] == "application/pdf"
    body = json.dumps({"id": "evt_9", "type": "checkout.session.completed",
                       "data": {"object": {**apis.get("/_admin/state", headers={"x-admin": "admin"}).json()["sessions"][0], "payment_status": "paid"}}}).encode()
    assert web.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": "t=1,v1=00"}).status_code == 400
    assert web.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": stripe_header("whsec_x", body)}).json()["result"] == "paid"
    assert 'data-stage="paid"' in web.get(f"/clients/{cid}").text
