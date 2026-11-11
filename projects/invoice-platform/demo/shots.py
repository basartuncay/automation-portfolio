"""Screenshots of the running stack for the README (real pages, the stack left up by ./e2e.sh).
Run: uv run --project ../js-scraper python demo/shots.py"""
from pathlib import Path

from playwright.sync_api import sync_playwright

DOCS, BASE = Path(__file__).parent.parent / "docs", "http://127.0.0.1:8080"

with sync_playwright() as p:
    pg = p.chromium.launch().new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
    pg.goto(BASE + "/admin/login")
    pg.fill("input[type=email]", "approver@example.com"); pg.fill("input[type=password]", "password")  # local demo seed account
    pg.click("button[type=submit]"); pg.wait_for_url("**/admin")
    pg.goto(BASE + "/admin/invoices"); pg.wait_for_selector("table"); pg.screenshot(path=DOCS / "inbox-invoices.png")
    pg.locator("tbody tr", has_text="INV-01042").first.locator("a").first.click()
    pg.get_by_role("tab", name="History").first.click(); pg.wait_for_timeout(800)
    pg.screenshot(path=DOCS / "invoice-history.png", full_page=True)
print("ok")
