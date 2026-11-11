"""Record a real walkthrough of the running stack for the demo video (no mock-ups). Prospects' replies are real
emails sent over SMTP to the sales inbox the worker reads over IMAP.

  docker compose down -v && POLL_SECONDS=1 DAY_SECONDS_SCALE=0.0001 docker compose up -d --build --wait
  uv run --project ../js-scraper python demo/record.py
  cd demo/video && npx remotion render Demo ../../docs/demo.mp4 && npx remotion still Thumbnail ../../docs/demo-thumb.png
"""
import email, email.policy, imaplib, smtplib, sys, time
from email.message import EmailMessage
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parents[2] / "tools"))
from demo_recorder import Recorder  # noqa: E402

APP = "http://127.0.0.1:8100"


def first_mail(addr):
    for _ in range(60):
        m = imaplib.IMAP4("127.0.0.1", 3145)
        try:
            m.login(addr, "x"); m.select("INBOX")
            ids = m.search(None, "ALL")[1][0].split()
            if ids:
                return email.message_from_bytes(m.fetch(ids[0], "(RFC822)")[1][0][1], policy=email.policy.default)
        finally:
            m.logout()
        time.sleep(0.5)
    raise SystemExit(f"no mail for {addr}")


def answer(orig, text, subject=None, **headers):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = orig["To"], "basar@outreach.example", subject or "Re: " + orig["Subject"]
    m["In-Reply-To"] = m["References"] = orig["Message-ID"]
    m["Message-ID"] = f"<{time.time_ns()}@prospect.example>"
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    m.set_content(text + "\n\nOn Wed, Basar Tuncay wrote:\n> Hi")
    with smtplib.SMTP("127.0.0.1", 3027) as s:
        s.send_message(m)


META = {"file": "leads.webm", "who": "Real stack · real SMTP/IMAP · local stand-ins for the data APIs and CRM", "accent": "#7c3aed",
        "kicker": "Lead Engine · real recording", "title": "From a CSV export to a booked call, without spamming anyone",
        "sub": "Merge, verify, enrich and score leads with reasons, send a short threaded sequence, read the replies, and hand only real interest to the CRM.",
        "outro": "Python · FastAPI · SQLite · SMTP/IMAP · HubSpot-style CRM API · Docker · e2e in GitHub Actions.",
        "thumbKicker": "OUTBOUND", "thumbTitle": "Leads scored with reasons, replies sorted", "thumbSub": "Lead Engine · import → sequence → CRM"}

with Recorder(HERE, META) as r:
    pg = r.page
    pg.goto(APP + "/login")
    pg.fill("input[name=email]", "growth@example.com"); pg.fill("input[name=password]", "password")
    r.click(pg.locator("button.btn"), 0.3)
    r.caption("An Apollo export with 20 rows: duplicates, a broken address, a shared inbox, a personal Gmail")
    r.click(pg.get_by_role("link", name="Imports"), 1)
    pg.set_input_files("input[type=file]", str(HERE.parent / "samples" / "apollo-export.csv"))
    r.click(pg.get_by_role("button", name="Import"), 0.5)
    r.caption("Columns recognised, 18 new leads, the duplicate merged, the broken row skipped, each with its reason")
    r.pause(4.5)
    r.caption("Each lead is verified and enriched by company domain, then scored against the ideal-customer rules")
    time.sleep(2)
    r.click(pg.get_by_role("link", name="Leads"), 4)
    r.caption("Every point is explained, so a salesperson can see why")
    r.click(pg.get_by_role("link", name="Luca Meier"), 1)
    r.move_to(pg.locator(".points"))
    r.pause(3.5)
    r.caption("Qualified leads get a personal first email, sent in office hours under a daily cap")
    lena = first_mail("lena@nordlicht-shop.de")
    first = {a: first_mail(a) for a in ("jonas.becker@kaffeekontor.de", "anna.gruber@bergwerk-outdoor.at")}
    pg.goto(APP + "/")
    r.pause(4)
    r.caption("The replies come in by email: one interested, one out of office until a date, one asking to stop")
    answer(lena, "Sounds good. Could you do Tuesday at 10:00?")
    answer(first["jonas.becker@kaffeekontor.de"], "Ich bin bis 30.12.2099 nicht im Büro.", subject="Automatische Antwort: Abwesend", Auto_Submitted="auto-replied")
    answer(first["anna.gruber@bergwerk-outdoor.at"], "Bitte austragen, danke.")
    time.sleep(3)
    pg.goto(APP + "/leads")
    r.pause(3.5)
    r.caption("Lena wants a call: her sequence stops and she goes to the CRM as a contact with a deal")
    r.click(pg.get_by_role("link", name="Lena Hoffmann"), 1)
    r.move_to(pg.locator(".email.in"))
    r.pause(4)
    r.caption("Out of office until a date: the next email waits until the day after")
    pg.goto(APP + "/leads?q=jonas")
    r.click(pg.get_by_role("link", name="Jonas Becker"), 3.5)
    r.caption("Asked to stop: suppressed for good, a later import can't bring her back")
    r.click(pg.get_by_role("link", name="Rules & sequence"), 1)
    pg.mouse.wheel(0, 1200)
    r.pause(3.5)
    r.caption("Everyone else gets the follow-ups as replies in the same thread")
    time.sleep(1)
    pg.goto(APP + "/")
    r.pause(4)
