"""Record a real walkthrough of the running stack for the demo video (no mock-ups). Customers on Shopify and Amazon
and the warehouse staff are played through the stand-ins' admin endpoints while the browser records the panel and
the channels' own quantities.

  docker compose down -v && AMAZON_POLL_SECONDS=2 RECONCILE_MINUTES=0 docker compose up -d --build --wait
  uv run --project ../js-scraper python demo/record.py
  cd demo/video && npx remotion render Demo ../../docs/demo.mp4 && npx remotion still Thumbnail ../../docs/demo-thumb.png
"""
import json, sys, time, urllib.error, urllib.request
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parents[2] / "tools"))
from demo_recorder import Recorder  # noqa: E402

ENV = dict(l.split("=", 1) for l in (HERE.parent / ".env").read_text().split())
HUB, APIS = "http://127.0.0.1:8102", "http://127.0.0.1:8103"


def admin(path, body=None):
    req = urllib.request.Request(APIS + path, json.dumps(body).encode() if body is not None else None,
                                 {"x-admin": ENV["FAKES_ADMIN_TOKEN"], "Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        return {"_status": e.code}


META = {"file": "stock.webm", "who": "Real stack · local stand-ins for Shopify, Amazon SP-API and the warehouse", "accent": "#16a34a",
        "kicker": "Stock Sync · real recording", "title": "Sell the same stock on Shopify and Amazon without overselling",
        "sub": "One stock ledger for both channels and the warehouse: bundles, safety buffers, sell-outs pushed in about a second, oversells caught.",
        "outro": "Python · FastAPI · SQLite · Shopify Admin API · Amazon SP-API · signed webhooks · Docker · e2e in GitHub Actions.",
        "thumbKicker": "E-COMMERCE", "thumbTitle": "One stock, every channel", "thumbSub": "Stock Sync · Shopify + Amazon + warehouse"}

with Recorder(HERE, META) as r:
    pg = r.page
    pg.goto(HUB + "/login")
    pg.fill("input[name=email]", "ops@example.com"); pg.fill("input[name=password]", "password")
    r.click(pg.locator("button.btn"), 0.3)
    r.caption("Each channel shows what's available, minus its own safety buffer. Bundles follow their scarcest part")
    r.pause(5)
    r.caption("A Shopify customer buys the last 3 kettles…")
    r.move_to(pg.locator('[data-sku="KETTLE-G"]').first)
    admin("/_admin/shopify/order", {"lines": [{"sku": "KETTLE-G", "qty": 3}]})
    r.pause(1.5)
    pg.reload()
    r.caption("…and Amazon shows 0 about a second later. The next Amazon buyer is refused")
    r.move_to(pg.locator('[data-channel="amazon"][data-sku="KETTLE-G"]'))
    assert admin("/_admin/amazon/order", {"lines": [{"sku": "AMZ-KETTLE-G", "qty": 1}]})["_status"] == 409
    r.pause(4)
    r.caption("The worst case: Shopify's webhook is late while Amazon sells the same grinders")
    slow = admin("/_admin/shopify/order", {"lines": [{"sku": "GRIND-H", "qty": 5}], "no_webhook": True})["order"]
    admin("/_admin/amazon/order", {"lines": [{"sku": "AMZ-GRIND-H", "qty": 2}]})
    time.sleep(3)
    admin("/_admin/shopify/webhook", {"topic": "orders/create", "id": slow["id"]})
    r.pause(1.5)
    r.click(pg.get_by_role("link", name="Exceptions"), 0.5)
    r.caption("Caught: the order can't be filled, and nothing is half-reserved. The exception says exactly what's short")
    r.pause(4.5)
    r.caption("The warehouse receives 10 grinders. One click, and the order is reserved")
    admin("/_admin/wms/receive", {"sku": "GRIND-H", "qty": 10})
    r.pause(1.5)
    r.click(pg.get_by_role("button", name="Try to reserve again"), 2.5)
    r.caption("Every change is a movement in the ledger, with its reference and the stock after it")
    pg.goto(HUB + "/sku/GRIND-H")
    r.pause(4.5)
    r.caption("Reconciliation: a stock count differs. It's only booked when it shows twice, so shipments in flight don't count")
    admin("/_admin/wms/count", {"sku": "MUG-WHT", "on_hand": 22})
    admin("/_admin/shopify/set", {"ref": "inv_1001", "qty": 99})
    r.click(pg.get_by_role("link", name="Reconciliation"), 0.5)
    r.click(pg.get_by_role("button", name="Run now"), 3)
    r.caption("And a quantity someone typed into Shopify by hand is found and put right")
    r.move_to(pg.get_by_text("showed 99"))
    r.pause(3.5)
    r.caption("Back to one truth on every channel")
    pg.goto(HUB + "/")
    r.pause(4)
