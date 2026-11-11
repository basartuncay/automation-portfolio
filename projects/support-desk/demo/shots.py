"""Screenshots of the running stack for the README (real pages, the stack left up by ./e2e.sh).
Run: uv run --project ../js-scraper python demo/shots.py"""
from pathlib import Path

from playwright.sync_api import sync_playwright

DOCS, BASE = Path(__file__).parent.parent / "docs", "http://127.0.0.1:8090"

with sync_playwright() as p:
    pg = p.chromium.launch().new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
    pg.goto(BASE + "/login")
    pg.fill("input[type=email]", "lead@example.com"); pg.fill("input[type=password]", "password"); pg.click("button.btn")  # local demo seed account
    pg.wait_for_url(BASE + "/")
    pg.screenshot(path=DOCS / "queue.png")
    pg.goto(BASE + "/?status=all")
    pg.click("tbody tr a:has-text('Express shipping')"); pg.screenshot(path=DOCS / "ticket.png")
    pg.goto(BASE + "/?status=all")
    pg.click("tbody tr a:has-text('Order 5521')"); pg.screenshot(path=DOCS / "injection.png")
print("ok")
