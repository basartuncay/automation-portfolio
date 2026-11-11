#!/usr/bin/env bash
# End-to-end in Docker: paid orders arrive by signed webhook; carrier rules pick DHL, DPD or DHL Express with customs;
# bad addresses and a missing HS code are held with the reason; a held order is fixed in the panel; labels are made
# once each; the day's batch prints as one PDF; printed parcels are fulfilled in the shop with tracking; carrier scans
# mark one delivered and one undeliverable; forged webhooks are refused.
set -euo pipefail
cd "$(dirname "$0")"
./setup.sh >/dev/null
docker compose down -v >/dev/null 2>&1 || true
POLL_SECONDS=1 docker compose up -d --build --wait --wait-timeout 300
trap '[ "${KEEP:-1}" = 0 ] && docker compose down -v >/dev/null 2>&1 || true' EXIT
set -a; . ./.env; set +a

python3 - <<'PY'
import json, os, re, time, urllib.error, urllib.parse, urllib.request
from http.cookiejar import CookieJar

APP, APIS = "http://127.0.0.1:8114", "http://127.0.0.1:8115"
H = {"x-admin": os.environ["FAKES_ADMIN_TOKEN"], "Content-Type": "application/json"}
admin = lambda path, body=None: json.loads(urllib.request.urlopen(urllib.request.Request(APIS + path, json.dumps(body).encode() if body is not None else None, H)).read())
web = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
get = lambda url: web.open(url).read().decode()
def post(url, page, **data):
    token = re.search(r'name="csrf" value="([^"]+)"', get(page)).group(1)
    r = web.open(url, urllib.parse.urlencode({**data, "csrf": token}).encode())
    return r.geturl(), r.read()
def wait(cond, what, tries=60):
    for _ in range(tries):
        if cond():
            return
        time.sleep(0.5)
    raise SystemExit(f"FAIL: timed out waiting for {what}")
DE = {"first_name": "Lena", "last_name": "Hoffmann", "address1": "Holtenauer Str. 12", "city": "Kiel", "zip": "24105", "country_code": "DE"}
orders = [({**DE}, [["EB-PRO", 1]], "standard"), ({**DE, "first_name": "Jonas", "address1": "Packstation 145", "address2": "Postnummer 1234567"}, [["HP-X200", 1]], "standard"),
          ({**DE, "first_name": "Sanne", "country_code": "NL", "zip": "1012 AB", "city": "Amsterdam"}, [["KB-K87", 1]], "standard"),
          ({**DE, "first_name": "Luca", "country_code": "CH", "zip": "8001", "city": "Zürich", "phone": "+41 44 123 45 67"}, [["SSD-1T", 2]], "standard"),
          ({**DE, "first_name": "Mia", "address1": "Holtenauer Strasse"}, [["CH-65W", 1]], "standard"),
          ({**DE, "first_name": "Tom", "country_code": "GB", "zip": "SW1A 1AA", "city": "London", "phone": "+44 20 7946 0000"}, [["EB-PRO", 1]], "express")]
for a, lines, ship in orders:
    assert admin("/_admin/order", {"address": a, "lines": lines, "shipping": ship})["webhook"] == 200
post(APP + "/login", APP + "/login", email="warehouse@example.com", password="password")
wait(lambda: len(admin("/_admin/state")["parcels"]) == 5, "labels for the 5 shippable orders")
parcels = {p["order_number"]: p for p in admin("/_admin/state")["parcels"]}
assert [parcels[n]["shipment"]["id"] for n in ("#1001", "#1002", "#1003", "#1004", "#1006")] == [8, 8, 5, 1103, 1103]
assert parcels["#1004"]["parcel_items"][0]["hs_code"] == "847170"
held = get(APP + "/?status=held")
assert 'data-order="#1005"' in held and "street has no house number" in held
print("PASS rules: DHL Warenpost, DHL to a Packstation, DPD in the EU, DHL Express with customs for Switzerland, express for the UK; a missing house number held")

oid = re.search(r'href="/orders/(\d+)"><b>#1005', held).group(1)
post(f"{APP}/orders/{oid}/address", f"{APP}/orders/{oid}", street="Holtenauer Str. 30", postcode="24105", city="Kiel", phone="")
wait(lambda: len(admin("/_admin/state")["parcels"]) == 6, "the fixed order's label")
print("PASS hold fixed: the house number added in the panel, the order re-checked and labelled")

url, _ = post(APP + "/batches", APP + "/?status=labelled")
bid = re.search(r"batch=(\d+)", url).group(1)
pdf = web.open(f"{APP}/batches/{bid}.pdf").read()
assert pdf.startswith(b"%PDF") and pdf.count(b"/Type /Page\n") + pdf.count(b"/Type /Page\r") + pdf.count(b"/Type /Page ") + pdf.count(b"/Type/Page") >= 6 or b"/Count 6" in pdf
wait(lambda: len(admin("/_admin/state")["fulfillments"]) == 6, "fulfilments in the shop")
f = admin("/_admin/state")["fulfillments"]
assert {x["tracking_company"] for x in f} == {"DHL", "DPD", "DHL Express"} and all(x["notify_customer"] for x in f)
print("PASS batch: 6 labels printed as one PDF; each order fulfilled in the shop with its carrier and tracking number")

admin("/_admin/scan", {"order_number": "#1001", "status": "in_transit"})
admin("/_admin/scan", {"order_number": "#1001", "status": "delivered"})
admin("/_admin/scan", {"order_number": "#1003", "status": "address_unknown", "message": "Recipient unknown at this address"})
wait(lambda: 'data-order="#1001"' in get(APP + "/?status=delivered"), "the delivered parcel")
wait(lambda: 'data-order="#1003"' in get(APP + "/?status=exception"), "the undeliverable parcel")
assert "can&#39;t find the address" in get(APP + "/?status=exception") or "can't find the address" in get(APP + "/?status=exception")
print("PASS tracking: carrier scans delivered one parcel and flagged one with an unknown recipient")

for path, h in (("/webhooks/shop", {"X-Shopify-Hmac-Sha256": "forged"}), ("/webhooks/carrier", {"Sendcloud-Signature": "forged"})):
    try:
        urllib.request.urlopen(urllib.request.Request(APP + path, b"{}", h))
        raise SystemExit(f"FAIL: forged {path} accepted")
    except urllib.error.HTTPError as e:
        assert e.code == 401
print("PASS webhooks: forged shop and carrier webhooks refused")
PY
