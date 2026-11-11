"""Screenshots of the running stack for the README (real pages, the stack left up by ./e2e.sh).
Run: uv run --project ../js-scraper python demo/shots.py"""
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

DOCS, APP = Path(__file__).parents[1] / "docs", "http://127.0.0.1:8104"
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
    pg.goto(APP + "/login")
    pg.fill("input[name=email]", "studio@example.com"); pg.fill("input[name=password]", "password"); pg.click("button.btn")
    pg.goto(APP + "/board"); pg.screenshot(path=DOCS / "board.png")
    pg.click("text=Nordlicht Shop"); pg.screenshot(path=DOCS / "client.png", full_page=True)
    pg.goto(APP + "/board"); pg.click("text=Slow Reply GmbH")
    link = pg.locator("a[href^='/p/']").first.get_attribute("href")
    c = b.new_page(viewport={"width": 900, "height": 1000}, device_scale_factor=2)
    c.goto(APP + link); c.fill("#typed", "Tom Weber"); c.screenshot(path=DOCS / "proposal.png", full_page=True)
    c.goto(APP + "/"); c.screenshot(path=DOCS / "enquiry.png", full_page=True)
    b.close()
print("ok")
