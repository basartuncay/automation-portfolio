#!/usr/bin/env bash
# End-to-end in Docker: Shopify, Amazon and the warehouse sharing one stock. Checks the first sync (buffers, bundles),
# a sell-out reaching the other channel in seconds, an oversell caught and fixed by a goods receipt, shipping,
# Amazon throttling, reconciliation of a stock count and a manual Shopify edit, and a forged warehouse webhook.
set -euo pipefail
cd "$(dirname "$0")"
./setup.sh >/dev/null
docker compose down -v >/dev/null 2>&1 || true
AMAZON_POLL_SECONDS=2 RECONCILE_MINUTES=0 docker compose up -d --build --wait --wait-timeout 300
trap '[ "${KEEP:-1}" = 0 ] && docker compose down -v >/dev/null 2>&1 || true' EXIT
set -a; . ./.env; set +a

python3 - <<'PY'
import json, os, re, time, urllib.error, urllib.parse, urllib.request
from http.cookiejar import CookieJar

HUB, APIS = "http://127.0.0.1:8102", "http://127.0.0.1:8103"
H = {"x-admin": os.environ["FAKES_ADMIN_TOKEN"], "Content-Type": "application/json"}
def admin(path, body=None):
    try:
        return json.loads(urllib.request.urlopen(urllib.request.Request(APIS + path, json.dumps(body).encode() if body is not None else None, H)).read())
    except urllib.error.HTTPError as e:
        return {"_status": e.code, **json.loads(e.read())}
state = lambda: admin("/_admin/state")
shop = lambda sku: state()["shopify"]["levels"][sku]
amz = lambda ref: state()["amazon"]["qty"][ref]
web = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
get = lambda url: web.open(url).read().decode()
def post(url, page, **data):
    token = re.search(r'name="csrf" value="([^"]+)"', get(page)).group(1)
    return web.open(url, urllib.parse.urlencode(data | {"csrf": token}).encode()).read().decode()
avail = lambda sku: int(re.search(rf'data-sku="{sku}" data-available="(\d+)"', get(HUB + "/")).group(1))
def wait(cond, what, tries=60, step=0.25):
    for _ in range(tries):
        if cond():
            return
        time.sleep(step)
    raise SystemExit(f"FAIL: timed out waiting for {what}")

post(HUB + "/login", HUB + "/login", email="ops@example.com", password="password")
wait(lambda: amz("AMZ-SET-STARTER") == 5 and shop("FILTER-100") == 50, "the first sync")
assert (shop("BEAN-1KG"), amz("AMZ-BEAN-1KG"), shop("SET-STARTER"), shop("MUG-PAIR")) == (18, 16, 6, 25)
print("PASS first sync: 13 listings pushed; Amazon keeps its safety buffer, filters capped at 50, bundles follow their scarcest part")

t0 = time.time()
r = admin("/_admin/shopify/order", {"lines": [{"sku": "KETTLE-G", "qty": 3}]})
assert r["webhook"] == 200, r
wait(lambda: amz("AMZ-KETTLE-G") == 0, "Amazon to show 0 kettles")
took = time.time() - t0
assert admin("/_admin/amazon/order", {"lines": [{"sku": "AMZ-KETTLE-G", "qty": 1}]})["_status"] == 409
print(f"PASS sell-out: last 3 kettles sold on Shopify, Amazon showed 0 after {took:.1f}s and refused the next buyer")

slow = admin("/_admin/shopify/order", {"lines": [{"sku": "GRIND-H", "qty": 5}], "no_webhook": True})["order"]  # its webhook is late
a = admin("/_admin/amazon/order", {"lines": [{"sku": "AMZ-GRIND-H", "qty": 2}]})
wait(lambda: avail("GRIND-H") == 4, "the Amazon order to be polled")
admin("/_admin/shopify/webhook", {"topic": "orders/create", "id": slow["id"]})
wait(lambda: "GRIND-H: needs 5, 4 available" in get(HUB + "/exceptions"), "the oversell exception")
assert avail("GRIND-H") == 4  # nothing half-reserved
admin("/_admin/wms/receive", {"sku": "GRIND-H", "qty": 10})
wait(lambda: avail("GRIND-H") == 14, "the goods receipt")
oid = re.search(r'action="/orders/(\d+)/retry"', get(HUB + "/exceptions")).group(1)
post(HUB + f"/orders/{oid}/retry", HUB + "/exceptions")
wait(lambda: avail("GRIND-H") == 9 and shop("SET-STARTER") == 9 and amz("AMZ-SET-STARTER") == 8, "the retried order and the bundle quantity")
print("PASS oversell: a late Shopify webhook found the stock sold on Amazon, raised an exception, reserved after the goods receipt; bundles followed")

admin("/_admin/wms/ship", {"order_ref": f"shopify:{slow['id']}", "units": {"GRIND-H": 5}})
wait(lambda: "11 on hand, 2 reserved" in get(HUB + "/sku/GRIND-H"), "the shipment")
assert avail("GRIND-H") == 9
print("PASS shipping: the warehouse shipment turned the reservation into a stock decrease, availability unchanged")

admin("/_admin/amazon/throttle", {"count": 3})
admin("/_admin/shopify/order", {"lines": [{"sku": "BEAN-1KG", "qty": 4}]})
wait(lambda: amz("AMZ-BEAN-1KG") == 12 and shop("BEAN-1KG") == 14, "Amazon to catch up after throttling")
assert "throttled" in get(HUB + "/log")
print("PASS throttling: Amazon answered 429 three times, the worker backed off and caught up")

admin("/_admin/wms/count", {"sku": "MUG-WHT", "on_hand": 22})
admin("/_admin/shopify/set", {"ref": "inv_1001", "qty": 99})
post(HUB + "/reconcile", HUB + "/reconcile")
page = get(HUB + "/reconcile")
assert re.search(r'data-recon="MUG-WHT">watching', page) and "showed 99, we had pushed 40" in page
wait(lambda: shop("MUG-BLK") == 40, "the manual Shopify edit to be overwritten")
post(HUB + "/reconcile", HUB + "/reconcile")
assert re.search(r'data-recon="MUG-WHT">adjusted', get(HUB + "/reconcile"))
wait(lambda: shop("MUG-WHT") == 22 and shop("MUG-PAIR") == 22, "the corrected count on Shopify")
print("PASS reconciliation: a count difference booked only after showing twice, a hand-edited Shopify quantity re-pushed")

r = admin("/_admin/wms/receive", {"sku": "MUG-BLK", "qty": 100, "signature": "forged"})
assert r["webhook"] == 401 and avail("MUG-BLK") == 40
print("PASS security: forged warehouse webhook refused, stock unchanged")
PY
