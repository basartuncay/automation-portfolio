"""Screenshots of the running stack for the README (real pages, the stack left up by ./e2e.sh).
Run: uv run --project ../js-scraper python demo/shots.py"""
from pathlib import Path

from playwright.sync_api import sync_playwright

DOCS, APP = Path(__file__).parents[1] / "docs", "http://127.0.0.1:8102"
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
    pg.goto(APP + "/login")
    pg.fill("input[name=email]", "ops@example.com"); pg.fill("input[name=password]", "password"); pg.click("button.btn")
    for name, path in [("stock", "/"), ("exceptions", "/exceptions"), ("reconcile", "/reconcile"), ("ledger", "/sku/GRIND-H"), ("orders", "/orders"), ("log", "/log")]:
        pg.goto(APP + path); pg.screenshot(path=DOCS / f"{name}.png")
    b.close()
print("ok")
