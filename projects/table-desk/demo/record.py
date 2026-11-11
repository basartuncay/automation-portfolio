"""Record a real walkthrough of the running stack for the demo video (no mock-ups). The phone agent's tool calls are
made the way a voice platform makes them; guests' text replies go through the SMS stand-in's signed webhook.

  docker compose down -v && POLL_SECONDS=1 docker compose up -d --build --wait
  uv run --project ../js-scraper python demo/record.py
  cd demo/video && npx remotion render Demo ../../docs/demo.mp4 && npx remotion still Thumbnail ../../docs/demo-thumb.png
"""
import json, sys, time, urllib.request
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parents[2] / "tools"))
from demo_recorder import Recorder  # noqa: E402

ENV = dict(l.split("=", 1) for l in (HERE.parent / ".env").read_text().split())
DESK, SMS = "http://127.0.0.1:8098", "http://127.0.0.1:8099"
d = date.today() + timedelta(days=3)
DAY = (d + timedelta(days=1) if d.weekday() == 0 else d).isoformat()


def call(url, body=None, headers=None):
    req = urllib.request.Request(url, json.dumps(body).encode() if body is not None else None, {"Content-Type": "application/json", **(headers or {})})
    return json.loads(urllib.request.urlopen(req).read())


def tool(name, args, cid, caller="+491715550101"):
    return call(f"{DESK}/agent/{name}", {"args": args, "call": {"call_id": cid, "from_number": caller}}, {"Authorization": "Bearer " + ENV["AGENT_TOOL_TOKEN"]})


reply = lambda frm, body: call(SMS + "/_admin/reply", {"from": frm, "body": body}, {"x-admin": ENV["FAKES_ADMIN_TOKEN"]})
phone = lambda n: f"{SMS}/phone/{n.replace('+', '%2B')}?token={ENV['FAKES_ADMIN_TOKEN']}"

META = {"file": "desk.webm", "who": "Real stack · local stand-ins for SMS", "accent": "#c9a14a", "bg": "#1e3a2f", "font": "Optima, Candara, sans-serif",
        "kicker": "Table Desk · real recording", "title": "Every reservation, from every channel, in one book",
        "sub": "An AI phone agent, a booking page, SMS and the staff panel share one engine: table joins, no double bookings, reminders and a waitlist.",
        "outro": "Python · FastAPI · SQLite · Twilio-style SMS · voice-agent tool webhooks · Docker · e2e in GitHub Actions.",
        "thumbKicker": "LOCAL BUSINESS", "thumbTitle": "Restaurant bookings that run themselves", "thumbSub": "Table Desk · phone agent + SMS + panel"}

with Recorder(HERE, META) as r:
    pg = r.page
    r.caption("A guest books on the restaurant's page")
    pg.goto(DESK + "/")
    r.pause(1.2)
    r.click(pg.locator("#party button[data-n='4']"), 0.8)
    pg.fill("#day", DAY); pg.dispatch_event("#day", "change")
    pg.wait_for_selector("#slots button:has-text('19:00')")
    r.click(pg.locator("#slots button:has-text('19:00')"), 0.5)
    r.type(pg.locator("#name"), "Jonas Keller")
    r.type(pg.locator("#phone"), "0160 9876543")
    r.caption("Only times with a free table are offered. The booking is checked again when it's saved")
    r.click(pg.locator("#go"), 2.5)
    r.caption("Booked, with a code and a link to cancel")
    r.pause(3)
    r.caption("The confirmation text arrives on the guest's phone")
    time.sleep(1.5)
    pg.goto(phone("+491609876543"))
    r.pause(3.5)

    r.caption("Meanwhile the AI phone agent takes a call for 8 people: it checks, books and confirms")
    tool("check_availability", {"date": DAY, "time": "19:30", "party_size": 8}, "call_501")
    tool("create_booking", {"name": "Kenji Sato", "date": DAY, "time": "19:30", "party_size": 8, "notes": "birthday"}, "call_501")
    tool("create_booking", {"name": "Kenji Sato", "date": DAY, "time": "19:30", "party_size": 8, "notes": "birthday"}, "call_501")  # the platform retried
    for i in range(8):
        tool("create_booking", {"name": f"Guest {i + 1}", "date": DAY, "time": "20:00", "party_size": 2, "phone": f"+4917166600{i:02d}"}, f"call_6{i:02d}")
    r.pause(3)
    pg.goto(DESK + "/login")
    pg.fill("input[name=email]", "host@example.com"); pg.fill("input[name=password]", "password")
    r.click(pg.locator("button.btn"), 0.5)
    pg.goto(f"{DESK}/panel?day={DAY}")
    r.pause(1)
    r.caption("Two tables joined for the party of 8. The platform's retry didn't book twice")
    r.move_to(pg.locator("a.blk", has_text="Kenji Sato").first)
    r.pause(3.5)
    r.caption("8 p.m. is full now. The phone agent puts the next caller on the waitlist")
    tool("join_waitlist", {"name": "Ada Berg", "phone": "+491718880000", "date": DAY, "earliest": "20:00", "latest": "20:30", "party_size": 2}, "call_700")
    r.click(pg.get_by_role("link", name="Waitlist"), 3)

    r.caption("A guest texts STORNO. The table is free, and Ada gets an offer by text")
    reply("+491716660000", "STORNO")
    time.sleep(2.5)
    pg.goto(phone("+491718880000"))
    r.pause(3)
    r.caption("Ada answers JA, and the table is hers")
    reply("+491718880000", "Ja, gerne!")
    r.pause(5)

    pg.goto(f"{DESK}/panel?day={DAY}")
    r.caption("The staff see the whole day: who booked how, cancellations and the waitlist booking")
    r.pause(2)
    pg.mouse.wheel(0, 700)
    r.pause(3.5)
    r.caption("Every text in and out, and anything that needs a person")
    r.click(pg.get_by_role("link", name="Messages"), 4)
