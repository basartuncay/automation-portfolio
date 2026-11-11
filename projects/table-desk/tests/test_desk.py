import re, threading, time
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from table_desk import db as store, engine
from table_desk.engine import Invalid, Unavailable
from table_desk.fakes import create_app as create_fakes, twilio_signature
from table_desk.web import create_app as create_web
from table_desk.worker import Gateway, send_due

ENV = {"SECRET_KEY": "s", "AGENT_TOOL_TOKEN": "agent-token", "SMS_AUTH_TOKEN": "sms-token", "SMS_ACCOUNT_SID": "AC1",
       "SMS_FROM": "+4972100000", "SMS_API_URL": "http://sms", "FAKES_ADMIN_TOKEN": "admin", "SMS_WEBHOOK_URL": "http://desk/webhooks/sms",
       "PUBLIC_URL": "http://desk", "DEMO_SEED": "true"}


@pytest.fixture
def conn(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "desk.db"))
    c = store.connect(str(tmp_path / "desk.db"))
    yield c
    c.close()


def day(offset=3) -> str:
    d = date.today() + timedelta(days=offset)
    return (d + timedelta(days=1) if d.weekday() == 0 else d).isoformat()  # Mondays are dinner only


def book(conn, time="19:00", party=2, channel="online", phone="+491711000000", name="Guest", d=None, **kw):
    return engine.book(conn, name=name, phone=phone, day=d or day(), time=time, party=party, channel=channel, who="test", **kw)


def tables_of(conn, b):
    return sorted(r[0] for r in conn.execute("select table_id from booking_tables where booking_id = ?", (b["id"],)))


def test_allocation_prefers_the_tightest_fit_then_joins_tables(conn):
    assert tables_of(conn, book(conn, party=2, phone="+491711000001")) == ["T1"]
    assert tables_of(conn, book(conn, party=5, phone="+491711000002")) == ["T7"]
    for i in range(3):
        assert len(tables_of(conn, book(conn, party=4, phone=f"+49171200000{i}"))) == 1
    assert tables_of(conn, book(conn, party=4, phone="+491711000003")) == ["T2", "T3"]  # all 4-tops taken: two 2-tops joined
    with pytest.raises(Invalid, match="call us"):
        book(conn, party=7)  # too big for online
    with pytest.raises(Unavailable) as e:
        book(conn, party=2, phone="+491711000009")  # bar seats are never handed out online
    assert e.value.alternatives  # ...but the guest is told what's still free
    assert tables_of(conn, book(conn, party=2, channel="staff", phone="+491711000009")) == ["B1"]


def test_tables_free_up_when_the_stay_ends(conn):
    first = book(conn, time="18:00", party=2, phone="+491711000001")
    assert tables_of(conn, first) == ["T1"] and first["end_"].endswith("19:30")
    assert tables_of(conn, book(conn, time="19:15", party=2, phone="+491711000002")) == ["T2"]
    assert tables_of(conn, book(conn, time="19:30", party=2, phone="+491711000003")) == ["T1"]


def test_parallel_bookings_never_share_a_table(conn, tmp_path):
    results = []

    def go(i):
        c = store.connect(str(tmp_path / "desk.db"))
        try:
            results.append(tables_of(c, engine.book(c, name=f"P{i}", phone=f"+4917130000{i:02d}", day=day(), time="19:00", party=6, channel="phone", who="t")))
        except Unavailable:
            results.append(None)
        finally:
            c.close()
    threads = [threading.Thread(target=go, args=(i,)) for i in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    won = [r for r in results if r]
    flat = [t for r in won for t in r]
    assert len(flat) == len(set(flat)), f"double booked: {won}"
    assert len(won) == 3  # T7, T1+T2+T3 and one of the 8-seat pairs, then the room is full for 6


def test_invalid_requests_are_explained(conn):
    with pytest.raises(Invalid, match="already passed"):
        book(conn, d=(date.today() - timedelta(days=1)).isoformat())
    with pytest.raises(Invalid, match="every 15 minutes"):
        book(conn, time="19:10")
    with pytest.raises(Invalid, match="Seating times"):
        book(conn, time="21:30")  # last seating is an hour before closing
    with pytest.raises(Invalid, match="days ahead"):
        book(conn, d=day(90))
    with pytest.raises(Invalid, match="phone number"):
        book(conn, phone="12")
    for t in ("18:00", "19:00", "20:00"):
        book(conn, time=t, phone="0171 4444444")  # national format is normalised to +49
    with pytest.raises(Invalid, match="open bookings"):
        book(conn, time="12:00", phone="+491714444444")


def test_confirmation_reminder_and_cancellation_messages(conn):
    b = book(conn)
    msgs = conn.execute("select kind, status, due from outbox where booking_id = ? order by id", (b["id"],)).fetchall()
    assert [m["kind"] for m in msgs] == ["confirmation", "reminder"]
    start = datetime.strptime(b["start"], "%Y-%m-%d %H:%M")
    assert abs(msgs[1]["due"] - (engine.epoch(store.settings(conn), start) - 24 * 3600)) < 1
    assert "STORNO" in conn.execute("select body from outbox where kind = 'confirmation'").fetchone()[0]
    engine.cancel(conn, b["id"], "test")
    assert [(m["kind"], m["status"]) for m in conn.execute("select kind, status from outbox where booking_id = ? order by id", (b["id"],))] == \
        [("confirmation", "skipped"), ("reminder", "skipped"), ("cancelled", "queued")]  # unsent ones are dropped


def test_sms_cancel_unknown_text_and_duplicate_delivery(conn):
    b = book(conn, phone="+491715550000")
    assert engine.handle_sms(conn, "+49 171 5550000", "Storno bitte", "SM1") == ""
    assert conn.execute("select status from bookings where id = ?", (b["id"],)).fetchone()[0] == "cancelled"
    engine.handle_sms(conn, "+491715550000", "STORNO", "SM1")  # webhook retried: nothing happens twice
    assert conn.execute("select count(*) from inbox").fetchone()[0] == 1
    assert "keine offene" in engine.handle_sms(conn, "+491715550000", "storno", "SM2")
    engine.handle_sms(conn, "+491715550000", "Können wir 30 min später kommen?", "SM3")
    row = conn.execute("select handled_as, done from inbox where sid = 'SM3'").fetchone()
    assert tuple(row) == ("staff", 0)  # free text is never answered by a guess: it waits for a person


def test_waitlist_offer_first_yes_wins(conn):
    d = day()
    holders = [book(conn, time="19:00", party=2, channel="staff", phone=f"+49171600000{i}", d=d) for i in range(5)]  # all 2-seaters
    for i, b in enumerate([4, 4, 4, 6]):
        book(conn, time="19:00", party=b, channel="staff", phone=f"+49171700000{i}", d=d)
    assert not engine.find_tables(conn, store.settings(conn), 2, datetime.strptime(f"{d} 19:00", "%Y-%m-%d %H:%M"), "phone")
    w1 = engine.join_waitlist(conn, name="Ada", phone="+491718000001", day=d, earliest="19:00", latest="19:30", party=2, who="t")
    w2 = engine.join_waitlist(conn, name="Bo", phone="+491718000002", day=d, earliest="19:00", latest="19:30", party=2, who="t")
    engine.cancel(conn, holders[0]["id"], "test")
    assert conn.execute("select status, offered_start from waitlist where id = ?", (w1,)).fetchone()[0] == "offered"
    assert conn.execute("select status from waitlist where id = ?", (w2,)).fetchone()[0] == "waiting"  # one live offer per day
    assert "JA" in conn.execute("select body from outbox where kind = 'offer'").fetchone()[0]
    engine.handle_sms(conn, "+491718000001", "Ja!", "SM10")
    w = conn.execute("select * from waitlist where id = ?", (w1,)).fetchone()
    assert w["status"] == "booked" and conn.execute("select channel from bookings where id = ?", (w["booking_id"],)).fetchone()[0] == "waitlist"
    # an offer that is never answered moves on to the next guest
    engine.cancel(conn, holders[1]["id"], "test")
    assert conn.execute("select status from waitlist where id = ?", (w2,)).fetchone()[0] == "offered"
    conn.execute("update waitlist set offer_expires = 0 where id = ?", (w2,))
    engine.expire_offers(conn)
    assert conn.execute("select status from waitlist where id = ?", (w2,)).fetchone()[0] == "expired"
    assert "abgelaufen" in engine.handle_sms(conn, "+491718000002", "ja", "SM11")


def test_offer_lost_to_a_walk_in_goes_back_to_waiting(conn):
    d = day()
    holders = [book(conn, time="20:00", party=2, channel="staff", phone=f"+49171600000{i}", d=d) for i in range(5)]
    for i, b in enumerate([4, 4, 4, 6]):
        book(conn, time="20:00", party=b, channel="staff", phone=f"+49171700000{i}", d=d)
    w = engine.join_waitlist(conn, name="Ada", phone="+491718000001", day=d, earliest="20:00", latest="20:00", party=2, who="t")
    engine.cancel(conn, holders[0]["id"], "test")
    book(conn, time="20:00", party=2, channel="staff", phone="+491719999999", d=d)  # staff gave the table away meanwhile
    assert "schneller" in engine.handle_sms(conn, "+491718000001", "JA", "SM20")
    assert conn.execute("select status from waitlist where id = ?", (w,)).fetchone()[0] == "waiting"


def test_worker_sends_retries_and_gives_up_on_bad_numbers(conn):
    fakes = TestClient(create_fakes(), base_url="http://sms")
    gw = Gateway(http=fakes)
    book(conn)
    engine.send(conn, "reply", "+1", "bad number")
    fakes.post("/_admin/fail", json={"count": 1}, headers={"x-admin": "admin"})
    send_due(conn, gw, max_attempts=3, backoff=0)  # first try hits the outage
    conn.execute("update outbox set due = 0 where status = 'queued' and kind != 'reminder'")
    send_due(conn, gw, max_attempts=3, backoff=0)
    rows = {r["kind"]: r for r in conn.execute("select * from outbox")}
    assert rows["confirmation"]["status"] == "sent" and rows["confirmation"]["attempts"] == 2
    assert rows["reply"]["status"] == "failed" and "21211" not in rows["reply"]["last_error"] and "not a valid" in rows["reply"]["last_error"]
    assert rows["reminder"]["status"] == "queued"  # not due yet
    sent = fakes.get("/_admin/messages", headers={"x-admin": "admin"}).json()
    assert len(sent) == 1 and "bestätigt" in sent[0]["body"]


def test_old_guest_data_is_removed(conn):
    b = book(conn)
    conn.execute("update bookings set start = '2020-01-01 19:00', end_ = '2020-01-01 20:30' where id = ?", (b["id"],))
    assert engine.anonymize(conn) == 1
    r = conn.execute("select name, phone, anonymized from bookings where id = ?", (b["id"],)).fetchone()
    assert tuple(r) == ("Guest", "", 1)


def agent(web, tool, args, call_id="call_1", token="agent-token"):
    return web.post(f"/agent/{tool}", json={"args": args, "call": {"call_id": call_id, "from_number": "+491719000000"}},
                    headers={"Authorization": f"Bearer {token}"})


def test_phone_agent_tools(conn):
    web = TestClient(create_web(secret="s"))
    assert agent(web, "check_availability", {"date": day(), "time": "19:00", "party_size": 4}, token="nope").status_code == 401
    r = agent(web, "check_availability", {"date": day(), "time": "19:00", "party_size": 4}).json()
    assert r["available"] and "Yes" in r["result"]
    args = {"name": "Kenji", "date": day(), "time": "19:00", "party_size": 8}
    first = agent(web, "create_booking", args).json()
    assert first["ok"] and agent(web, "create_booking", args).json() == first  # the platform retried: still one booking
    assert conn.execute("select count(*) from bookings").fetchone()[0] == 1
    assert conn.execute("select phone from bookings").fetchone()[0] == "+491719000000"  # caller ID used when no number is given
    r = agent(web, "create_booking", {"name": "Big", "date": day(), "time": "19:00", "party_size": 12}).json()
    assert not r["ok"] and "call us" in r["result"]
    assert "8 people" in agent(web, "find_booking", {}).json()["result"]
    assert agent(web, "cancel_booking", {"date": day()}, call_id="call_2").json()["ok"]
    assert agent(web, "take_message", {"name": "Lena", "message": "Allergy question"}).json()["ok"]
    assert agent(web, "create_booking", {"date": day()}).json()["result"].startswith("I'm missing")


def test_sms_webhook_checks_the_signature(conn):
    web = TestClient(create_web(secret="s"), base_url="http://desk")
    book(conn, phone="+491715550000")
    form = {"MessageSid": "SM9", "From": "+491715550000", "Body": "STORNO"}
    assert web.post("/webhooks/sms", data=form, headers={"X-Twilio-Signature": "forged"}).status_code == 403
    assert web.post("/webhooks/sms", data=form, headers={"X-Twilio-Signature": twilio_signature("sms-token", "http://desk/webhooks/sms", form)}).status_code == 200
    assert conn.execute("select status from bookings").fetchone()[0] == "cancelled"


def test_online_booking_page_flow(conn, monkeypatch):
    monkeypatch.setenv("BOOKINGS_PER_HOUR", "1")
    web = TestClient(create_web(secret="s"))
    assert "19:00" in web.get(f"/api/slots?day={day()}&party=2").json()["times"]
    assert web.get(f"/api/slots?day={day()}&party=9").status_code == 422
    assert web.post("/api/book", json={"website": "spam"}).json()["code"] == "XXXXXX"  # honeypot
    assert conn.execute("select count(*) from bookings").fetchone()[0] == 0
    r = web.post("/api/book", json={"name": "Mia", "phone": "+491712223344", "day": day(), "time": "19:00", "party": 2}).json()
    assert r["ok"]
    page = web.get(r["manage"])
    token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    assert "last digits of the booking" in web.post(r["manage"] + "/cancel", data={"phone_end": "0000", "csrf": token}).text
    assert "is cancelled" in web.post(r["manage"] + "/cancel", data={"phone_end": "3344", "csrf": token}).text
    assert web.post("/api/book", json={"name": "Mia", "phone": "+491712223344", "day": day(), "time": "19:00", "party": 2}).status_code == 429


def test_panel_day_view_and_actions(conn):
    web = TestClient(create_web(secret="s"), follow_redirects=False)
    assert web.get("/panel").headers["location"] == "/login"
    token = re.search(r'name="csrf" value="([^"]+)"', web.get("/login").text).group(1)
    web.post("/login", data={"email": "host@example.com", "password": "password", "csrf": token})
    b = book(conn, name="Rosa", party=4)
    page = web.get(f"/panel?day={day()}").text
    token = re.search(r'name="csrf" value="([^"]+)"', page).group(1)  # a new token after sign-in
    assert "Rosa" in page and 'class="blk booked online"' in page
    r = web.post("/panel/bookings", data={"csrf": token, "day": day(), "time": "19:00", "party": "2", "name": "Walk", "walk_in": "1", "tables": "B1"})
    assert r.status_code == 303 and "Booked+Walk" in r.headers["location"], r.text
    web.post(f"/panel/bookings/{b['id']}", data={"csrf": token, "action": "no_show"})
    assert conn.execute("select status from bookings where id = ?", (b["id"],)).fetchone()[0] == "no_show"
    assert "no_show" in web.get(f"/panel/bookings/{b['id']}").text
    for p in ("/panel/waitlist", "/panel/messages", "/panel/setup"):
        assert web.get(p).status_code == 200
