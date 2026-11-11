"""Record a real walkthrough of the running stack for the demo video (no mock-ups): both statements are uploaded
through the panel, a person settles the unclear payments, and the reminders are run.

  docker compose down -v && POLL_SECONDS=1 DUNNING_AT=off docker compose up -d --build --wait
  uv run python -m paymatch.demo_data samples/ && uv run --project ../js-scraper python demo/record.py
  cd demo/video && npx remotion render Demo ../../docs/demo.mp4 && npx remotion still Thumbnail ../../docs/demo-thumb.png
"""
import json, sys, urllib.request
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parents[2] / "tools"))
from demo_recorder import Recorder  # noqa: E402

ENV = dict(l.split("=", 1) for l in (HERE.parent / ".env").read_text().split())
APP, APIS = "http://127.0.0.1:8106", "http://127.0.0.1:8107"
urllib.request.urlopen(urllib.request.Request(APIS + "/_admin/chaos", json.dumps({"fail": 2}).encode(),
                                              {"x-admin": ENV["FAKES_ADMIN_TOKEN"], "Content-Type": "application/json"}))
META = {"file": "paymatch.webm", "who": "Real stack · real mail server · local stand-in for the accounting system", "accent": "#6fb592", "bg": "#13294b", "font": "Charter, Georgia, serif",
        "kicker": "Paymatch · real recording", "title": "Bank statement in, invoices settled, reminders only where due",
        "sub": "CAMT.053 and CSV statements matched to open invoices with explained scores, booked in accounting when sure, the rest reviewed by a person.",
        "outro": "Python · FastAPI · SQLite · CAMT.053 · defusedxml · accounting API · SMTP · Docker · e2e in GitHub Actions.",
        "thumbKicker": "FINANCE", "thumbTitle": "Payments matched, with reasons", "thumbSub": "Paymatch · CAMT.053 → invoices → reminders"}

with Recorder(HERE, META) as r:
    pg = r.page
    pg.goto(APP + "/login")
    pg.fill("input[name=email]", "finance@example.com"); pg.fill("input[name=password]", "password")
    r.click(pg.locator("button.btn"), 1.5)
    r.caption("This morning's bank statement as CAMT.053, the XML format German and EU banks export")
    pg.set_input_files("input[type=file]", str(HERE.parent / "samples" / "statement.camt053.xml"))
    r.click(pg.get_by_role("button", name="Import statement"), 1)
    r.caption("9 bookings read. 6 payments matched and booked on their own, through a short outage of the accounting API")
    r.pause(2.5)
    pg.reload()
    r.move_to(pg.locator("span.chip.paid", has_text="booked").first)
    r.pause(2)
    r.caption("Every match says why: a discount taken, a bank fee kept, two invoices in one transfer, a part payment")
    for kind in ("skonto", "fee", "multi", "partial"):
        r.move_to(pg.locator(f'[data-kind$=":{kind}"]').first)
        r.pause(1.4)
    r.caption("The bank's CSV export overlaps the CAMT file. The same bookings aren't imported twice")
    pg.set_input_files("input[type=file]", str(HERE.parent / "samples" / "umsaetze.csv"))
    r.click(pg.get_by_role("button", name="Import statement"), 4)
    r.caption("Two payments weren't clear enough. €560 from a private name fits two invoices equally")
    r.click(pg.get_by_role("link", name="To review"), 3.5)
    r.caption("A person picks the right one. The marketplace payout isn't an invoice payment at all")
    r.click(pg.locator("form.sugg", has_text="RE-2026-0107").get_by_role("button", name="Confirm"), 1.5)
    r.click(pg.get_by_role("button", name="Not an invoice payment"), 2)
    r.caption("Reminders go only to customers who really owe money, for what's still open")
    r.click(pg.get_by_role("link", name="Invoices & reminders"), 1)
    r.click(pg.get_by_role("button", name="Run reminders now"), 2)
    pg.mouse.wheel(0, 700)
    r.pause(4)
