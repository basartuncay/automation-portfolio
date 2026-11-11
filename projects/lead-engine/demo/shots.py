"""Screenshots of the running stack for the README (real pages, the stack left up by ./e2e.sh).
Run: uv run --project ../js-scraper python demo/shots.py"""
from pathlib import Path

from playwright.sync_api import sync_playwright

DOCS, APP = Path(__file__).parents[1] / "docs", "http://127.0.0.1:8100"
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
    pg.goto(APP + "/login")
    pg.fill("input[name=email]", "growth@example.com"); pg.fill("input[name=password]", "password"); pg.click("button.btn")
    pg.goto(APP + "/"); pg.screenshot(path=DOCS / "pipeline.png")
    pg.goto(APP + "/leads"); pg.screenshot(path=DOCS / "leads.png")
    pg.goto(APP + "/leads?q=lena"); pg.click("text=Lena Hoffmann"); pg.screenshot(path=DOCS / "lead.png", full_page=True)
    pg.goto(APP + "/leads?q=luca"); pg.click("text=Luca Meier"); pg.screenshot(path=DOCS / "lead-unsubscribed.png")
    pg.goto(APP + "/imports"); pg.click("text=apollo-export.csv"); pg.screenshot(path=DOCS / "import.png")
    pg.goto(APP + "/setup"); pg.screenshot(path=DOCS / "setup.png")
    b.close()
print("ok")
