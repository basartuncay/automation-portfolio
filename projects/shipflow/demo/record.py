"""Record a real walkthrough of the running stack for the demo video (no mock-ups). Customers' orders and the
carrier's scans come in as signed webhooks from the stand-ins.

  docker compose down -v && POLL_SECONDS=1 docker compose up -d --build --wait
  uv run --project ../js-scraper python demo/record.py
  cd demo/video && npx remotion render Demo ../../docs/demo.mp4 && npx remotion still Thumbnail ../../docs/demo-thumb.png
"""
import json, shutil, sys, time, urllib.request
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parents[2] / "tools"))
from demo_recorder import Recorder  # noqa: E402

ENV = dict(l.split("=", 1) for l in (HERE.parent / ".env").read_text().split())
APP, APIS = "http://127.0.0.1:8114", "http://127.0.0.1:8115"
H = {"x-admin": ENV["FAKES_ADMIN_TOKEN"], "Content-Type": "application/json"}
admin = lambda path, body=None: json.loads(urllib.request.urlopen(urllib.request.Request(APIS + path, json.dumps(body).encode() if body is not None else None, H)).read())
DE = {"first_name": "Lena", "last_name": "Hoffmann", "address1": "Holtenauer Str. 12", "city": "Kiel", "zip": "24105", "country_code": "DE"}
ORDERS = [({**DE}, [["EB-PRO", 1]], "standard"), ({**DE, "first_name": "Jonas", "last_name": "Becker", "address1": "Packstation 145", "address2": "Postnummer 1234567", "city": "Hamburg", "zip": "20095"}, [["HP-X200", 1]], "standard"),
          ({**DE, "first_name": "Sanne", "last_name": "de Vries", "country_code": "NL", "zip": "1012 AB", "city": "Amsterdam", "address1": "Damrak 70"}, [["KB-K87", 1]], "standard"),
          ({**DE, "first_name": "Luca", "last_name": "Meier", "country_code": "CH", "zip": "8001", "city": "Zürich", "address1": "Bahnhofstrasse 21", "phone": "+41 44 123 45 67"}, [["SSD-1T", 2]], "standard"),
          ({**DE, "first_name": "Mia", "last_name": "Wagner", "address1": "Lindenallee", "city": "Berlin", "zip": "10115"}, [["CH-65W", 1]], "standard"),
          ({**DE, "first_name": "Tom", "last_name": "Hughes", "country_code": "GB", "zip": "SW1A 1AA", "city": "London", "address1": "10 Whitehall", "phone": "+44 20 7946 0000"}, [["EB-PRO", 1]], "express")]
META = {"file": "shipflow.webm", "who": "Real stack · local stand-ins for a Sendcloud-style carrier API and the shop", "accent": "#d97706",
        "kicker": "Shipflow · real recording", "title": "From paid order to tracked parcel, without the copy-paste",
        "sub": "Carrier rules with reasons, address and customs checks, one label per order, the day's labels in one PDF, tracking back to the shop and problems surfaced.",
        "outro": "Python · FastAPI · SQLite · Sendcloud-style carrier API · Shopify fulfillments · signed webhooks · Docker · e2e in GitHub Actions.",
        "thumbKicker": "E-COMMERCE OPS", "thumbTitle": "Labels, customs and tracking, handled", "thumbSub": "Shipflow · order → label → tracking"}

with Recorder(HERE, META) as r:
    pg = r.page
    pg.goto(APP + "/login")
    pg.fill("input[name=email]", "warehouse@example.com"); pg.fill("input[name=password]", "password")
    r.click(pg.locator("button.btn"), 0.5)
    r.caption("Six paid orders arrive from the shop. Each is checked and gets a carrier by rule")
    for a, lines, ship in ORDERS:
        admin("/_admin/order", {"address": a, "lines": lines, "shipping": ship})
        time.sleep(0.3)
    time.sleep(1.5)
    pg.goto(APP + "/?status=labelled")
    r.pause(2)
    r.caption("DHL Warenpost, DHL to a Packstation, DPD in the EU, DHL Express with customs data. Labels made automatically")
    r.move_to(pg.locator("tr[data-order='#1004']"))
    r.pause(3.5)
    r.caption("One order is on hold: the street has no house number. Fixed here, re-checked, labelled")
    r.click(pg.get_by_role("link", name="On hold"), 1)
    r.click(pg.locator("a", has_text="#1005").first, 1)
    pg.locator("input[name=street]").fill("")
    r.type(pg.locator("input[name=street]"), "Lindenallee 4")
    r.click(pg.get_by_role("button", name="Fix and re-check"), 2.5)
    r.caption("End of the day: all new labels in one PDF for the printer")
    pg.goto(APP + "/?status=labelled")
    r.pause(1)
    r.click(pg.get_by_role("button", name="Print new labels"), 2.5)
    r.caption("Printed parcels are marked shipped in the shop with carrier and tracking. The shop emails the customer")
    time.sleep(1.5)
    r.click(pg.get_by_role("link", name="Shipped"), 3.5)
    r.shot("shipped.png")
    r.caption("The carrier scans parcels. One is delivered; one address can't be found, and lands under Problems")
    admin("/_admin/scan", {"order_number": "#1001", "status": "delivered"})
    admin("/_admin/scan", {"order_number": "#1003", "status": "address_unknown", "message": "Recipient unknown at this address"})
    time.sleep(1)
    r.click(pg.get_by_role("link", name="Problems"), 2.5)
    r.click(pg.locator("a", has_text="#1003").first, 3.5)
    r.shot("problem.png")
    r.caption("Every decision explained, every step in the history")
    r.click(pg.get_by_role("link", name="Carrier rules"), 3.5)
    r.shot("rules.png")
for f in ("shipped.png", "problem.png", "rules.png"):
    shutil.copy(HERE / "out" / f, HERE.parent / "docs" / f)
shutil.copy(HERE / "out" / "shipped.png", HERE / "video" / "public" / "queue.png")
