"""Screenshots for the README from a fresh stack (real pages):
  docker compose down -v && POLL_SECONDS=1 DUNNING_AT=off docker compose up -d --build --wait
  uv run python -m paymatch.demo_data samples/ && uv run --project ../js-scraper python demo/shots.py"""
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
DOCS, APP = HERE.parent / "docs", "http://127.0.0.1:8106"
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
    pg.goto(APP + "/login")
    pg.fill("input[name=email]", "finance@example.com"); pg.fill("input[name=password]", "password"); pg.click("button.btn")
    pg.wait_for_timeout(2500)  # the worker mirrors the open invoices
    pg.set_input_files("input[type=file]", str(HERE.parent / "samples" / "statement.camt053.xml")); pg.click("text=Import statement")
    pg.wait_for_timeout(2500)
    pg.goto(APP + "/"); pg.screenshot(path=DOCS / "payments.png")
    pg.goto(APP + "/review"); pg.screenshot(path=DOCS / "review.png")
    pg.goto(APP + "/invoices"); pg.click("text=Run reminders now"); pg.screenshot(path=DOCS / "invoices.png")
    b.close()
print("ok")
