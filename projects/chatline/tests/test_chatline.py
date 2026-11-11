import hashlib, hmac, json, re, time

import pytest
from fastapi.testclient import TestClient

from chatline import bot, db as store
from chatline.clients import Shop, WhatsApp, buttons
from chatline.fakes import create_app as create_fakes
from chatline.web import create_app as create_web

ENV = {"SECRET_KEY": "s", "WA_ACCESS_TOKEN": "tok", "WA_APP_SECRET": "appsecret", "WA_VERIFY_TOKEN": "verify", "WA_PHONE_NUMBER_ID": "1001",
       "FAKES_ADMIN_TOKEN": "admin", "SHOP_TOKEN": "shop", "SHOP_WEBHOOK_SECRET": "shophook", "WA_WEBHOOK_URL": "http://unreachable.invalid/webhooks/whatsapp",
       "SHOP_WEBHOOK_URL": "http://unreachable.invalid/webhooks/shop", "GRAPH_URL": "http://apis/graph", "SHOP_URL": "http://apis/shop", "DEMO_SEED": "true"}
LENA, MAX = "4917612345678", "4917699887766"
A = {"x-admin": "admin"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "c.db"))
    conn = store.connect(str(tmp_path / "c.db"))
    apis = TestClient(create_fakes(), base_url="http://apis")
    yield conn, apis, WhatsApp(http=apis), Shop(http=apis)
    conn.close()


def wrap(msg, name="Lena"):
    return {"entry": [{"changes": [{"value": {"contacts": [{"profile": {"name": name}, "wa_id": msg["from"]}], "messages": [msg]}}]}]}


class Customer:
    def __init__(self, env, wa_id, name="Lena"):
        self.conn, self.apis, self.wa, self.shop = env
        self.id, self.name = wa_id, name

    def _go(self, r):
        bot.handle_webhook(self.conn, self.wa, self.shop, wrap(r["msg"], self.name))
        return r["msg"]

    def says(self, text, age_hours=0):
        return self._go(self.apis.post("/_admin/say", json={"from": self.id, "name": self.name, "text": text, "age_hours": age_hours}, headers=A).json())

    def taps(self, rid, title="", lst=False):
        return self._go(self.apis.post("/_admin/tap", json={"from": self.id, "id": rid, "title": title, "list": lst}, headers=A).json())

    def last(self):
        return self.apis.get(f"/_admin/chat/{self.id}", headers=A).json()[-1]["msg"]

    def last_text(self):
        m = self.last()
        return m["text"]["body"] if m["type"] == "text" else m["interactive"]["body"]["text"]


def test_menu_tracking_by_phone_and_list(env):
    lena = Customer(env, LENA)
    lena.says("hi")
    assert [b["reply"]["id"] for b in lena.last()["interactive"]["action"]["buttons"]] == ["track", "return", "human"]
    lena.taps("track", "Track my order")
    rows = lena.last()["interactive"]["action"]["sections"][0]["rows"]
    assert [r["id"] for r in rows] == ["track:1042", "track:1031"]  # two orders on her number: a list to pick from
    lena.taps("track:1042", "Order 1042", lst=True)
    assert "on its way with DHL" in lena.last_text() and "track.example.com/1042" in lena.last_text()
    lena.says("where is my order?")  # free text is understood too
    assert "Which order" in lena.last_text()


def test_order_number_needs_the_postcode(env):
    stranger = Customer(env, "4915550000000", "Someone")
    stranger.taps("track", "Track my order")
    assert "order number" in stranger.last_text()
    stranger.says("Order #1050")
    assert "postcode" in stranger.last_text()
    stranger.says("10115")  # wrong
    assert "doesn't match" in stranger.last_text()
    stranger.says("99999")  # wrong again: a person takes it
    conv = env[0].execute("select mode from conversations where wa_id = '4915550000000'").fetchone()
    assert conv["mode"] == "human" and "1050" not in stranger.last_text()
    owner = Customer(env, "4915550000001", "Someone else")
    owner.taps("track", "Track my order"); owner.says("1050"); owner.says("80 331")
    assert "being packed" in owner.last_text()


def test_return_flow_is_idempotent(env):
    conn, apis, *_ = env
    lena = Customer(env, LENA)
    lena.taps("return", "Return an item")
    rows = lena.last()["interactive"]["action"]["sections"][0]["rows"]
    assert [r["id"] for r in rows] == ["ret:1031"]  # only delivered orders within 30 days
    lena.taps("ret:1031", "Order 1031", lst=True)
    lena.taps("item:SNK-42", "Runner sneakers", lst=True)
    lena.taps("size", "Wrong size")
    assert "R-1031-1" in lena.last_text() and "returns.example.com/label" in lena.last_text()
    assert conn.execute("select outcome from conversations").fetchone()[0] == "return created"
    # the same choice again (a double tap) doesn't create a second return
    conn.execute("update conversations set state = 'pick_reason', data = ?", (json.dumps({"order": "1031", "sku": "SNK-42"}),))
    lena.taps("size", "Wrong size")
    assert len(apis.get("/_admin/returns", headers=A).json()) == 1
    old = Customer(env, MAX, "Max")
    old.taps("return", "Return an item")
    assert "last 30 days" in old.last_text()  # order 0977 was delivered 55 days ago


def test_handoff_keeps_the_bot_quiet_and_unclear_goes_to_a_person(env):
    conn, apis, wa, shop = env
    lena = Customer(env, LENA)
    lena.says("asdfgh")
    assert "didn't get that" in lena.last_text()
    lena.says("qwerty")
    conv = conn.execute("select * from conversations").fetchone()
    assert conv["mode"] == "human" and conv["unread"] == 1
    n = len(apis.get(f"/_admin/chat/{LENA}", headers=A).json())
    lena.says("hello? where is my order")
    assert len(apis.get(f"/_admin/chat/{LENA}", headers=A).json()) == n + 1  # only her message: the bot stayed silent
    assert bot.agent_reply(conn, wa, conv["id"], "Hi Lena, it's on its way!", "Cleo")
    bot.close(conn, conv["id"], "Cleo")
    lena.says("hi again")
    assert conn.execute("select count(*) from conversations").fetchone()[0] == 2 and "help" in lena.last_text()


def test_window_templates_and_opt_in(env):
    conn, apis, wa, shop = env
    lena = Customer(env, LENA)
    lena.says("hi", age_hours=30)  # her last message was 30 hours ago
    conv = conn.execute("select id from conversations").fetchone()[0]
    assert not bot.agent_reply(conn, wa, conv, "Hello?", "Cleo")
    assert "24-hour" in conn.execute("select error from messages order by id desc").fetchone()[0]
    order = shop.order("1042")
    assert bot.shipped(conn, wa, order) == "not opted in"
    lena.says("please don't stop the delivery")  # not a keyword in a sentence
    assert bot.shipped(conn, wa, order) == "not opted in"
    lena.says("Thanks! START")
    assert bot.shipped(conn, wa, order) == "sent"
    assert apis.get(f"/_admin/chat/{LENA}", headers=A).json()[-1]["msg"]["template"]["name"] == "order_shipped"
    lena.says("stop")
    assert bot.shipped(conn, wa, order) == "not opted in"


def test_webhook_signature_verification_dedupe_and_statuses(env):
    conn, apis, wa, shop = env
    web = TestClient(create_web(secret="s", wa=wa, shop=shop))
    assert web.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "verify", "hub.challenge": "42"}).text == "42"
    assert web.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"}).status_code == 403
    msg = apis.post("/_admin/say", json={"from": LENA, "name": "Lena", "text": "hi"}, headers=A).json()["msg"]
    body = json.dumps(wrap(msg)).encode()
    sig = "sha256=" + hmac.new(b"appsecret", body, hashlib.sha256).hexdigest()
    assert web.post("/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": "sha256=00"}).status_code == 401
    assert web.post("/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": sig}).json()["messages"] == 1
    assert web.post("/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": sig}).json()["duplicates"] == 1  # Meta retried
    assert conn.execute("select count(*) from messages where direction = 'out'").fetchone()[0] == 1
    wamid = conn.execute("select wamid from messages where direction = 'out'").fetchone()[0]
    st = json.dumps({"entry": [{"changes": [{"value": {"statuses": [{"id": wamid, "status": "read", "recipient_id": LENA}]}}]}]}).encode()
    web.post("/webhooks/whatsapp", content=st, headers={"X-Hub-Signature-256": "sha256=" + hmac.new(b"appsecret", st, hashlib.sha256).hexdigest()})
    assert conn.execute("select status from messages where wamid = ?", (wamid,)).fetchone()[0] == "read"
    bot.record_status(conn, wamid, "delivered", None)  # a late, older status doesn't move it back
    assert conn.execute("select status from messages where wamid = ?", (wamid,)).fetchone()[0] == "read"
    bot.record_status(conn, "wamid.early", "delivered", None)  # a status before the message row exists is kept
    conn.execute("insert into messages (conversation_id, direction, wamid, kind, body, author, at) values (1, 'out', 'wamid.early', 'text', 'x', 'bot', 'now')")
    bot.apply_status(conn, "wamid.early")
    assert conn.execute("select status from messages where wamid = 'wamid.early'").fetchone()[0] == "delivered"


def test_whatsapp_limits_in_builders():
    with pytest.raises(AssertionError):
        buttons("x", [("a", "1"), ("b", "2"), ("c", "3"), ("d", "4")])
    assert buttons("x", [("a", "A very long button title indeed")])["interactive"]["action"]["buttons"][0]["reply"]["title"] == "A very long button ti"[:20]


def test_panel(env):
    conn, apis, wa, shop = env
    lena = Customer(env, LENA)
    lena.taps("human", "Talk to a person")
    web = TestClient(create_web(secret="s", wa=wa, shop=shop), follow_redirects=False)
    token = re.search(r'name="csrf" value="([^"]+)"', web.get("/login").text).group(1)
    web.post("/login", data={"email": "care@example.com", "password": "password", "csrf": token})
    page = web.get("/").text
    assert f'data-conv="{LENA}" data-mode="human"' in page
    cid = conn.execute("select id from conversations").fetchone()[0]
    token = re.search(r'name="csrf" value="([^"]+)"', web.get(f"/c/{cid}").text).group(1)
    web.post(f"/c/{cid}/reply", data={"csrf": token, "body": "Hi Lena, Cleo here."})
    assert apis.get(f"/_admin/chat/{LENA}", headers=A).json()[-1]["msg"]["text"]["body"] == "Hi Lena, Cleo here."
    assert "data-mode=\"closed\"" not in web.get(f"/c/{cid}").text
    web.post(f"/c/{cid}/mode", data={"csrf": token, "to": "closed"})
    assert conn.execute("select mode from conversations").fetchone()[0] == "closed"
