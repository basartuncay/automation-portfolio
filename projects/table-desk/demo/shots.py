"""Screenshots of the running stack for the README (real pages, the stack left up by ./e2e.sh).
Run: uv run --project ../js-scraper python demo/shots.py"""
import re
from datetime import date, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
ENV = dict(l.split("=", 1) for l in (HERE.parent / ".env").read_text().split())
DOCS, DESK, SMS = HERE.parent / "docs", "http://127.0.0.1:8098", "http://127.0.0.1:8099"
d = date.today() + timedelta(days=3)
DAY = (d + timedelta(days=1) if d.weekday() == 0 else d).isoformat()

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
    pg.goto(DESK + "/login")
    pg.fill("input[name=email]", "host@example.com"); pg.fill("input[name=password]", "password"); pg.click("button.btn")
    pg.goto(f"{DESK}/panel?day={DAY}"); pg.screenshot(path=DOCS / "day-view.png")
    pg.goto(f"{DESK}/panel/messages"); pg.screenshot(path=DOCS / "messages.png")
    pg.goto(f"{DESK}/panel/waitlist"); pg.screenshot(path=DOCS / "waitlist.png")
    m = b.new_page(viewport={"width": 430, "height": 900}, device_scale_factor=2)
    m.goto(DESK + "/"); m.fill("#day", DAY); m.dispatch_event("#day", "change")
    m.wait_for_selector("#slots button"); m.click("#slots button:has-text('18:30')")
    m.fill("#name", "Jonas Keller"); m.fill("#phone", "+49 160 9876543")
    m.screenshot(path=DOCS / "booking-page.png", full_page=True)
    m.click("#go"); m.wait_for_selector("#done:not([hidden])"); m.screenshot(path=DOCS / "booked.png")
    ph = b.new_page(viewport={"width": 420, "height": 720}, device_scale_factor=2)
    ph.goto(f"{SMS}/phone/{'+491718880000'.replace('+', '%2B')}?token={ENV['FAKES_ADMIN_TOKEN']}"); ph.screenshot(path=DOCS / "guest-phone.png")
    b.close()
print("ok")
