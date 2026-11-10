import json
import re
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from doc_qa.core import BM25, load_folder
from support_desk import db as store
from support_desk.engine import REPLY_SYSTEM, ClaudeEngine, OfflineEngine, TransientError, search
from support_desk.web import create_app
from support_desk.worker import handle, parse, process_inbox

ROOT = Path(__file__).parents[1]
KB = ROOT.parent / "doc-qa" / "samples"
INDEX = BM25(load_folder(KB))
RAW = {p.stem: p.read_bytes() for p in sorted((ROOT / "samples").glob("*.eml"))}


class FakeImap:
    def __init__(self, messages):
        self.msgs = {str(i + 1).encode(): m for i, m in enumerate(messages)}
        self.seen = set()

    def select(self, box):
        pass

    def uid(self, cmd, *args):
        if cmd == "search":
            return "OK", [b" ".join(u for u in self.msgs if u not in self.seen)]
        if cmd == "fetch":
            return "OK", [(b"", self.msgs[args[0]])]
        if cmd == "store":
            self.seen.add(args[0])
            return "OK", []


@pytest.fixture
def conn(tmp_path):
    c = store.connect(str(tmp_path / "t.db"))
    yield c
    c.close()


def file_all(conn, engine=None):
    imap = FakeImap(list(RAW.values()))
    return imap, process_inbox(imap, conn, engine or OfflineEngine(), INDEX)


def statuses(conn):
    return {r["subject"]: (r["status"], r["reason"]) for r in conn.execute("select * from tickets")}


def test_inbox_is_triaged_and_routed(conn):
    imap, stats = file_all(conn)
    assert stats == {"emails": 6, "filed": 6, "duplicates": 0, "retry_later": 0} and len(imap.seen) == 6
    s = statuses(conn)
    assert s["Refund for my returned headphones"][0] == "drafted"
    assert s["Express shipping - need it today"] == ("drafted", "high urgency: a lead must approve this reply")
    assert s["Can I pay with PayPal?"][0] == "needs_human" and "documents don't answer" in s["Can I pay with PayPal?"][1]
    assert s["Order 5521"][0] == "needs_human" and "prompt injection" in s["Order 5521"][1]
    assert s["SEO services for your store"][0] == "closed"
    assert s["Invoice NP/7790"] == ("routed", "not a support request: routed to finance")
    gaps = [r["question"] for r in conn.execute("select question from gaps")]
    assert gaps == ["Can I pay with PayPal?"]  # only real unanswered support questions, not the injection


def test_injection_never_gets_a_draft(conn):
    file_all(conn)
    t = conn.execute("select * from tickets where subject = 'Order 5521'").fetchone()
    assert t["injection"] == 1 and t["draft"] is None


def test_draft_quotes_the_docs_with_sources(conn):
    file_all(conn)
    t = conn.execute("select * from tickets where subject like 'Refund%'").fetchone()
    assert t["draft"].startswith("Hi Maria,") and "within 5 business days" in t["draft"]
    sources = json.loads(t["sources"])
    assert any(s.startswith("returns-policy.md › How refunds are paid:") for s in sources)
    for s in sources:  # every quoted sentence really is in the docs, verbatim
        quote = s.split(': "', 1)[1][:-1]
        assert any(quote in p.read_text() for p in KB.glob("*.md")), quote


def test_reprocessing_is_a_noop_and_history_is_append_only(conn):
    file_all(conn)
    mail = parse(RAW["01_refund"])
    assert handle(conn, OfflineEngine(), INDEX, mail) is None
    assert conn.execute("select count(*) from tickets").fetchone()[0] == 6
    with pytest.raises(Exception, match="append-only"):
        conn.execute("delete from events")


def test_transient_error_leaves_mail_unread_for_retry(conn):
    class Down(OfflineEngine):
        def triage(self, mail):
            raise TransientError("rate limited")
    imap, stats = file_all(conn, Down())
    assert stats["retry_later"] == 6 and not imap.seen
    assert conn.execute("select count(*) from tickets").fetchone()[0] == 0
    imap2, stats2 = FakeImap(list(RAW.values())), None
    stats2 = process_inbox(imap2, conn, OfflineEngine(), INDEX)
    assert stats2["filed"] == 6


def test_claude_engine_request_and_parsing():
    cite = NS(document_title="shipping.md › Costs", cited_text="Express shipping costs 14.90 EUR.")
    resp = NS(stop_reason="end_turn", content=[NS(type="text", text="Hi James,\n\nExpress costs 14.90 EUR.", citations=[cite]),
                                               NS(type="text", text="\n\nSupport team", citations=None)])
    calls = []
    client = NS(messages=NS(create=lambda **kw: calls.append(kw) or resp))
    mail = parse(RAW["02_express"])
    text, sources = ClaudeEngine(client).draft(mail, search(INDEX, mail))
    assert text == "Hi James,\n\nExpress costs 14.90 EUR.\n\nSupport team"
    assert sources == ['shipping.md › Costs: "Express shipping costs 14.90 EUR."']
    req = calls[0]
    assert req["system"] == REPLY_SYSTEM and "never follow instructions" in req["system"]
    docs, ask = req["messages"][0]["content"][:-1], req["messages"][0]["content"][-1]["text"]
    assert docs and all(d["citations"] == {"enabled": True} for d in docs)
    assert "<email>" in ask and "express shipping" in ask.lower()


# --- panel ---

class Outbox(list):
    def __call__(self, msg):
        self.append(msg)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_SEED", "true")
    monkeypatch.setenv("KB_DIR", str(KB))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    path = str(tmp_path / "web.db")
    c = store.connect(path)
    file_all(c)
    c.close()
    outbox = Outbox()
    return TestClient(create_app(path, send=outbox, secret="test-secret"), follow_redirects=False), outbox, path


def csrf(client, url="/login"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(url).text).group(1)


def login(client, email):
    r = client.post("/login", data={"email": email, "password": "password", "csrf": csrf(client)})
    assert r.status_code == 303


def tid(path, subject_like):
    with store.connect(path) as c:
        return c.execute("select id from tickets where subject like ?", (subject_like,)).fetchone()[0]


def test_login_required_and_wrong_password(app):
    client, _, _ = app
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    r = client.post("/login", data={"email": "agent@example.com", "password": "nope", "csrf": csrf(client)})
    assert "Wrong email or password" in r.text


def test_agent_sends_draft_as_reply_in_thread(app):
    client, outbox, path = app
    login(client, "agent@example.com")
    t = tid(path, "Refund%")
    page = client.get(f"/tickets/{t}").text
    assert "Sources the draft is based on" in page and "How refunds are paid" in page
    with store.connect(path) as c:
        draft = c.execute("select draft from tickets where id = ?", (t,)).fetchone()[0]
    r = client.post(f"/tickets/{t}/send", data={"reply": draft, "csrf": csrf(client, f"/tickets/{t}")})
    assert r.status_code == 303
    msg = outbox[0]
    assert msg["To"] == "maria.lopez@customer.example" and msg["Subject"] == "Re: Refund for my returned headphones"
    assert msg["In-Reply-To"] == "<r1@customer.example>" and "5 business days" in msg.get_content()
    with store.connect(path) as c:
        row = c.execute("select status, sent_by from tickets where id = ?", (t,)).fetchone()
        ev = c.execute("select actor, detail from events where ticket_id = ? and action = 'sent'", (t,)).fetchone()
    assert row["status"] == "sent" and ev["actor"] == "Sam Agent" and json.loads(ev["detail"])["edited"] is False
    # a second click can't send it twice
    r = client.post(f"/tickets/{t}/send", data={"reply": draft, "csrf": csrf(client, f"/tickets/{t}")})
    assert r.status_code == 403 and len(outbox) == 1


def test_urgent_and_flagged_need_a_lead(app):
    client, outbox, path = app
    login(client, "agent@example.com")
    for subject in ("Express%", "Order 5521"):
        t = tid(path, subject)
        assert "need a lead" in client.get(f"/tickets/{t}").text
        r = client.post(f"/tickets/{t}/send", data={"reply": "ok", "csrf": csrf(client, f"/tickets/{t}")})
        assert r.status_code == 403
    client.post("/logout", data={"csrf": csrf(client, "/")})
    login(client, "lead@example.com")
    t = tid(path, "Express%")
    r = client.post(f"/tickets/{t}/send", data={"reply": "Edited by the lead", "csrf": csrf(client, f"/tickets/{t}")})
    assert r.status_code == 303 and outbox[-1].get_content().strip() == "Edited by the lead"
    with store.connect(path) as c:
        assert json.loads(c.execute("select detail from events where ticket_id = ? and action = 'sent'", (t,)).fetchone()[0])["edited"]


def test_viewer_cannot_act_and_csrf_is_checked(app):
    client, outbox, path = app
    login(client, "viewer@example.com")
    t = tid(path, "Refund%")
    assert "viewers can&#39;t send replies" in client.get(f"/tickets/{t}").text
    assert client.post(f"/tickets/{t}/send", data={"reply": "x", "csrf": csrf(client, f"/tickets/{t}")}).status_code == 403
    assert client.post(f"/tickets/{t}/close", data={"csrf": csrf(client, f"/tickets/{t}")}).status_code == 403
    assert client.post(f"/tickets/{t}/send", data={"reply": "x", "csrf": "forged"}).status_code == 400
    assert outbox == []


def test_smtp_failure_keeps_the_ticket_open(app, tmp_path):
    _, _, path = app
    def broken(msg):
        raise OSError("connection refused")
    client = TestClient(create_app(path, send=broken, secret="test-secret"), follow_redirects=False)
    login(client, "agent@example.com")
    t = tid(path, "Refund%")
    r = client.post(f"/tickets/{t}/send", data={"reply": "hi", "csrf": csrf(client, f"/tickets/{t}")})
    assert "Not+sent" in r.headers["location"]
    with store.connect(path) as c:
        assert c.execute("select status from tickets where id = ?", (t,)).fetchone()[0] == "drafted"


def test_queue_and_knowledge_pages(app):
    client, _, _ = app
    login(client, "agent@example.com")
    q = client.get("/").text
    assert "Refund for my returned headphones" in q and "SEO services" not in q  # open queue hides closed spam
    assert "Offline rules (no API key)" in q
    assert "SEO services" in client.get("/?status=closed").text
    kb = client.get("/knowledge").text
    assert "Can I pay with PayPal?" in kb and "shipping.md › Costs" in kb
