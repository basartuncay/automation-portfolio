"""Record a real walkthrough of the running stack for the demo video (no mock-ups). The product's usage is sent to
the real API; the end of the billing period and the card network are played through the Stripe stand-in.

  docker compose down -v && REPORT_SECONDS=1 GRACE_DAYS=0.0001 docker compose up -d --build --wait
  uv run --project ../js-scraper python demo/record.py
  cd demo/video && npx remotion render Demo ../../docs/demo.mp4 && npx remotion still Thumbnail ../../docs/demo-thumb.png
"""
import json, re, shutil, sys, time, urllib.request
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parents[2] / "tools"))
from demo_recorder import Recorder  # noqa: E402

ENV = dict(l.split("=", 1) for l in (HERE.parent / ".env").read_text().split())
APP, STRIPE = "http://127.0.0.1:8112", "http://127.0.0.1:8113"
H = {"x-admin": ENV["FAKES_ADMIN_TOKEN"], "Content-Type": "application/json"}
admin = lambda path, body=None: json.loads(urllib.request.urlopen(urllib.request.Request(STRIPE + path, json.dumps(body).encode() if body is not None else None, H)).read())
api = lambda key, body: json.loads(urllib.request.urlopen(urllib.request.Request(APP + "/v1/events", json.dumps(body).encode(),
                                                                                  {"Authorization": f"Bearer {key}", "Content-Type": "application/json"})).read())
ev = lambda n, k: {"metric": "api_calls", "quantity": n, "idempotency_key": k}
META = {"file": "meter.webm", "who": "Real stack · local stand-in for Stripe Billing", "accent": "#635bff",
        "kicker": "Meter · real recording", "title": "Usage-based billing on Stripe, without surprises",
        "sub": "Usage counted once, reported to Stripe's meter and reconciled, alerts before overage, spend caps, and failed payments handled with a grace period.",
        "outro": "Python · FastAPI · SQLite · Stripe Billing (meters, subscriptions, signed webhooks) · Docker · e2e in GitHub Actions.",
        "thumbKicker": "SAAS BILLING", "thumbTitle": "Usage billing that adds up", "thumbSub": "Meter · usage → Stripe → invoices"}

with Recorder(HERE, META) as r:
    pg = r.page
    pg.goto(APP + "/login")
    pg.fill("input[name=email]", "billing@example.com"); pg.fill("input[name=password]", "password")
    r.click(pg.locator("button.btn"), 0.5)
    r.caption("A new customer on the Starter plan: created in Stripe with a subscription, API key shown once")
    r.type(pg.locator("input[name=name]"), "Nordlicht Shop")
    r.type(pg.locator("input[name=email]"), "billing@nordlicht.example")
    r.click(pg.get_by_role("button", name="Create in Stripe"), 2.5)
    key = pg.locator("#new-key").inner_text()
    r.caption("The product sends usage. A batch retried after a timeout is counted once")
    for i, n in enumerate([1800, 2600, 3100, 1500]):
        api(key, {"events": [ev(n, f"b{i}")]})
        api(key, {"events": [ev(n, f"b{i}")]}) if i == 1 else None
        time.sleep(0.6)
    pg.reload()
    r.pause(3)
    r.caption("Past 80% of the included calls: the customer gets an email. Then usage runs over")
    api(key, {"events": [ev(2500, "b9")]})
    r.click(pg.locator("a", has_text="Nordlicht Shop").first, 1)
    r.move_to(pg.locator(".ent"))
    r.pause(3.5)
    r.caption("They set a spend cap. When the bill reaches it, the product is told to pause usage")
    pg.fill("input[name=cap]", "")
    r.type(pg.locator("input[name=cap]"), "35")
    r.click(pg.get_by_role("button", name="Save cap"), 1)
    api(key, {"events": [ev(1000, "b10")]})
    pg.reload()
    r.move_to(pg.locator(".ent"))
    r.pause(3)
    r.caption("Every event went to Stripe's meter. Reconciliation: our count and Stripe's agree")
    r.click(pg.get_by_role("link", name="Stripe reconciliation"), 3)
    cus = next(c["id"] for c in admin("/_admin/state")["customers"].values())
    admin("/_admin/card", {"customer": cus, "ok": False})
    inv = admin("/_admin/close_period", {"customer": cus})
    r.caption("The period closes. The card is declined: past due, and after the grace period, paused")
    time.sleep(1)
    r.click(pg.get_by_role("link", name="Customers", exact=True), 0.5)
    r.click(pg.locator("a", has_text="Nordlicht Shop").first, 1)
    time.sleep(8)
    pg.reload()
    r.move_to(pg.locator(".ent"))
    r.pause(3)
    r.caption("A new card, Stripe retries, the invoice is paid, and access is back at once")
    admin("/_admin/card", {"customer": cus, "ok": True})
    admin("/_admin/retry", {"invoice": inv["id"]})
    time.sleep(1)
    pg.reload()
    r.move_to(pg.locator(f'[data-invoice="{inv["number"]}:paid"]'))
    r.pause(3.5)
    r.shot("customer.png")
    r.caption("And every email the customer got along the way")
    r.click(pg.get_by_role("link", name="Emails to customers"), 4)
    r.shot("emails.png")
    pg.goto(APP + "/")
    r.shot("customers.png")
for f in ("customer.png", "emails.png", "customers.png"):
    shutil.copy(HERE / "out" / f, HERE.parent / "docs" / f)
shutil.copy(HERE / "out" / "customer.png", HERE / "video" / "public" / "queue.png")
