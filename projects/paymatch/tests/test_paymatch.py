import json, re
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from paymatch import bank, db as store, match
from paymatch.accounting import Accounting, dunning, push, sync
from paymatch.demo_data import camt, german_csv
from paymatch.fakes import create_app as create_fakes
from paymatch.web import create_app as create_web

A = {"x-admin": "admin"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in {"SECRET_KEY": "s", "ACCOUNTING_TOKEN": "t", "FAKES_ADMIN_TOKEN": "admin", "ACCOUNTING_URL": "http://x/accounting",
                 "DEMO_SEED": "true", "DB_PATH": str(tmp_path / "p.db")}.items():
        monkeypatch.setenv(k, v)
    conn = store.connect(str(tmp_path / "p.db"))
    fakes = TestClient(create_fakes(), base_url="http://x")
    api = Accounting(http=fakes)
    sync(conn, api)
    yield conn, fakes, api
    conn.close()


def loaded(conn):
    bank.store(conn, "s.xml", camt().encode(), "t")
    return match.run(conn)


def by_number(conn):
    rows = conn.execute("select i.number, m.kind, m.amount_cents, m.writeoff_cents, m.auto from matches m join invoices i on i.id = m.invoice_id").fetchall()
    return {r["number"]: tuple(r)[1:] for r in rows}


def test_parsers_and_duplicates(env):
    conn, *_ = env
    tx = bank.parse_camt(camt().encode())
    assert len(tx) == 9 and tx[1]["amount_cents"] == 233240 and tx[7]["amount_cents"] == -250000 and tx[0]["name"] == "NORDLICHT SHOP GMBH"
    rows = bank.parse_csv(german_csv().encode("cp1252"))
    assert [r["amount_cents"] for r in rows] == [119000, 233240, 32000] and rows[2]["name"] == "Lumen Lighting B.V."
    assert bank.store(conn, "s.xml", camt().encode(), "t")["new"] == 9
    assert bank.store(conn, "s.xml", camt().encode(), "t")["new"] == 0
    r = bank.store(conn, "u.csv", german_csv().encode("cp1252"), "t")
    assert (r["format"], r["new"], r["duplicates"]) == ("csv", 1, 2)  # overlap with the CAMT file recognised
    with pytest.raises(bank.BadStatement):
        bank.store(conn, "x.csv", b"hello;world\n1;2\n", "t")
    bomb = b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]><Document>&b;</Document>'
    with pytest.raises(bank.BadStatement):
        bank.parse_camt(bomb)  # entity expansion is refused


def test_matching_kinds(env):
    conn, *_ = env
    assert loaded(conn) == {"auto": 6, "parked": 2}
    m = by_number(conn)
    assert m["RE-2026-0101"] == ("exact", 119000, 0, 1)
    assert m["RE-2026-0102"] == ("skonto", 233240, 4760, 1)          # 2% early-payment discount written off
    assert m["RE-2026-0103"] == ("fee", 83250, 750, 1)               # €7.50 kept by the payer's bank
    assert m["RE-2026-0104"][0] == m["RE-2026-0105"][0] == "multi"   # one transfer, two invoices
    assert m["RE-2026-0106"] == ("partial", 40000, 0, 1)
    assert conn.execute("select open_cents, status from invoices where number = 'RE-2026-0106'").fetchone()[:] == (60000, "partial")
    assert "RE-2026-0107" not in m  # €560 from a private name, no reference: two invoices fit equally, a person decides
    sug = json.loads(conn.execute("select suggestions from transactions where name = 'Mia Wagner'").fetchone()[0])
    assert {tuple(s["numbers"]) for s in sug} == {("RE-2026-0107",), ("RE-2026-0110",)}
    assert conn.execute("select status from transactions where amount_cents < 0").fetchone()[0] == "outgoing"


def test_review_decisions(env):
    conn, *_ = env
    loaded(conn)
    t = conn.execute("select * from transactions where name = 'Mia Wagner'").fetchone()
    pick = next(i for i, s in enumerate(json.loads(t["suggestions"])) if s["numbers"] == ["RE-2026-0107"])
    match.apply(conn, t["id"], json.loads(t["suggestions"])[pick], auto=False, who="Frida")
    assert by_number(conn)["RE-2026-0107"][-1] == 0
    amazon = conn.execute("select id from transactions where name like 'AMAZON%'").fetchone()[0]
    match.manual(conn, amazon, ["inv_108"], "Frida")  # €89 towards a €1,500 invoice: partial, oldest first
    assert conn.execute("select open_cents from invoices where id = 'inv_108'").fetchone()[0] == 150000 - 8900


def test_booking_is_idempotent_and_sync_never_reopens(env):
    conn, fakes, api = env
    loaded(conn)
    sync(conn, api)  # before booking: local open amounts must stay reduced
    assert conn.execute("select open_cents from invoices where number = 'RE-2026-0101'").fetchone()[0] == 0
    fakes.post("/_admin/chaos", json={"fail": 1}, headers=A)
    assert push(conn, api) == 0 and conn.execute("select count(*) from matches where booking_error like '503%'").fetchone()[0] == 1
    assert push(conn, api) == 7  # 6 payments, one of them over two invoices
    state = fakes.get("/_admin/state", headers=A).json()
    assert state["inv_102"]["open_cents"] == 0 and state["inv_102"]["payments"][0]["writeoff_cents"] == 4760
    assert state["inv_106"]["open_cents"] == 60000
    conn.execute("update matches set booked = 0")  # as if the answers were lost: the retry is recognised by external_id
    assert push(conn, api) == 7
    assert len(fakes.get("/_admin/state", headers=A).json()["inv_101"]["payments"]) == 1
    sync(conn, api)
    assert conn.execute("select open_cents from invoices where number = 'RE-2026-0106'").fetchone()[0] == 60000
    fakes.post("/_admin/pay_elsewhere", json={"id": "inv_108"}, headers=A)
    assert sync(conn, api)["closed_elsewhere"] == 1


def test_dunning_levels_and_holds(env):
    conn, *_ = env
    loaded(conn)
    first = dunning(conn)
    # overdue with money open: 0106 (rest after a part payment), 0108, 0109. 0107 is held: a payment waiting for review may be it
    assert sorted(first) == ["RE-2026-0106: level 1", "RE-2026-0108: level 1", "RE-2026-0109: level 1"]
    assert dunning(conn) == []  # never two letters within a week
    assert dunning(conn, today=date.today() + timedelta(days=8)) == []  # no new statement since: paused
    conn.execute("update transactions set booked = ? where id = 1", ((date.today() + timedelta(days=8)).isoformat(),))
    later = dunning(conn, today=date.today() + timedelta(days=8))
    assert "RE-2026-0108: level 2" in later
    body = conn.execute("select body from outbox where subject like 'Second%'").fetchone()[0]
    assert "34 days overdue" in body


def test_panel(env):
    conn, fakes, api = env
    web = TestClient(create_web(secret="s"), follow_redirects=False)
    token = re.search(r'name="csrf" value="([^"]+)"', web.get("/login").text).group(1)
    web.post("/login", data={"email": "finance@example.com", "password": "password", "csrf": token})
    token = re.search(r'name="csrf" value="([^"]+)"', web.get("/").text).group(1)
    r = web.post("/import", data={"csrf": token}, files={"file": ("s.xml", camt().encode(), "application/xml")})
    assert "6+matched+automatically" in r.headers["location"].replace("%20", "+")
    page = web.get("/").text
    assert 'data-kind="RE-2026-0102:skonto"' in page and "2% discount taken" in page
    review = web.get("/review").text
    tid = re.search(r'data-tx="(\d+)"', review).group(1)
    web.post(f"/review/{tid}", data={"csrf": token, "pick": "0"})
    assert conn.execute("select status from transactions where id = ?", (tid,)).fetchone()[0] == "matched"
    assert "data-inv" in web.get("/invoices").text
    bad = web.post("/import", data={"csrf": token}, files={"file": ("x.csv", b"nothing here", "text/csv")})
    assert "error=" in bad.headers["location"]
