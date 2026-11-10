#!/usr/bin/env bash
# End-to-end in Docker: connect the CRM over OAuth through the panel, then check every path of a two-way sync:
# store -> CRM, CRM -> store, one-way fields, no echo loops, a conflict resolved in the panel, an outage that ends in
# a dead letter and is replayed, a forged webhook rejected, and an expired token refreshed.
set -euo pipefail
cd "$(dirname "$0")"
./setup.sh >/dev/null
docker compose down -v >/dev/null 2>&1 || true
MAX_ATTEMPTS=3 BACKOFF_SECONDS=0.5 POLL_SECONDS=1 docker compose up -d --build --wait --wait-timeout 300
trap '[ "${KEEP:-1}" = 0 ] && docker compose down -v >/dev/null 2>&1 || true' EXIT
set -a; . ./.env; set +a

python3 - <<'PY'
import json, os, re, time, urllib.error, urllib.parse, urllib.request
from http.cookiejar import CookieJar

HUB, APIS = "http://127.0.0.1:8094", "http://127.0.0.1:8095"
ADMIN = {"x-admin": os.environ["FAKES_ADMIN_TOKEN"], "Content-Type": "application/json"}
web = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
get = lambda url: web.open(url).read().decode()
def post(url, page, **data):
    token = re.search(r'name="csrf" value="([^"]+)"', get(page)).group(1)
    return web.open(url, urllib.parse.urlencode(data | {"csrf": token}).encode()).read().decode()
def admin(path, body=None):
    req = urllib.request.Request(APIS + path, json.dumps(body).encode() if body is not None else None, ADMIN)
    return json.loads(urllib.request.urlopen(req).read())
crm = lambda: admin("/crm/_admin/state")
shop = lambda: admin("/shop/_admin/state")
def wait(cond, what, tries=40):
    for _ in range(tries):
        if cond():
            return
        time.sleep(0.5)
    raise SystemExit(f"FAIL: timed out waiting for {what}")
contact = lambda email: next((c for c in crm()["contacts"] if c["properties"].get("email") == email), None)
customer = lambda email: next((c for c in shop()["customers"] if c["email"] == email), None)

post(HUB + "/login", HUB + "/login", email="ops@example.com", password="password")
consent = post(HUB + "/connect/crm", HUB + "/")  # redirected to the CRM's consent screen
fields = dict(re.findall(r'name="(redirect_uri|state)" value="([^"]+)"', consent))
assert "wants to read and update your <b>contacts</b>" in consent
landing = web.open(APIS + "/crm/oauth/authorize", urllib.parse.urlencode(fields).encode()).read().decode()
assert "CRM connected" in landing
print("PASS oauth: consent screen, code exchange, tokens stored")

ana = admin("/shop/_admin/customers", {"email": "ana.silva@example.com", "first_name": "Ana", "last_name": "Silva", "phone": "+351 912 345 678"})
wait(lambda: contact("ana.silva@example.com"), "the new store customer in the CRM")
assert contact("ana.silva@example.com")["properties"]["phone"] == "+351912345678"
cid = contact("ana.silva@example.com")["id"]
admin(f"/crm/_admin/contacts/{cid}", {"lastname": "Silva Costa", "vip": "true"})  # a sales rep edits the CRM...
admin("/shop/_admin/orders", {"customer_id": ana["id"], "amount": 89.9})            # ...while the customer orders
wait(lambda: customer("ana.silva@example.com")["last_name"] == "Silva Costa", "the CRM edit in the store")
wait(lambda: contact("ana.silva@example.com")["properties"].get("lifecyclestage") == "customer", "the order in the CRM")
assert customer("ana.silva@example.com")["tags"] == "VIP" and contact("ana.silva@example.com")["properties"]["total_spent"] == "89.90"
print("PASS two-way: store->CRM create, simultaneous CRM edit + store order both arrive, one-way fields respected")

time.sleep(3)
writes = (crm()["stats"]["writes"], shop()["stats"]["writes"])
time.sleep(4)  # several poll cycles and webhook echoes later
assert (crm()["stats"]["writes"], shop()["stats"]["writes"]) == writes, "the hub is looping"
print("PASS no loops: write counters stable over 4 s of polling")

admin(f"/shop/_admin/customers/{ana['id']}", {"phone": "+351 900 000 001", "silent": True})
admin(f"/crm/_admin/contacts/{cid}", {"phone": "+351 900 000 002"})
wait(lambda: "Keep store value" in get(HUB + "/conflicts"), "the conflict")
assert customer("ana.silva@example.com")["phone"] == "+351 900 000 001"
cfid = re.search(r'action="/conflicts/(\d+)"', get(HUB + "/conflicts")).group(1)
post(HUB + f"/conflicts/{cfid}", HUB + "/conflicts", winner="shop")
wait(lambda: contact("ana.silva@example.com")["properties"]["phone"] == "+351900000001", "the resolution in the CRM")
print("PASS conflict: neither side overwritten, store value kept from the panel")

admin("/crm/_admin/fail", {"mode": "503", "count": 999})
admin("/shop/_admin/customers", {"email": "zoe@example.com", "first_name": "Zoe"})
wait(lambda: "Replay all" in get(HUB + "/dead"), "the dead letter")
assert "503 from the server" in get(HUB + "/dead") and not contact("zoe@example.com")
admin("/crm/_admin/fail", {"mode": "ok"})
post(HUB + "/dead/replay", HUB + "/dead", event_id="all")
wait(lambda: contact("zoe@example.com"), "the replayed customer")
print("PASS outage: retried, parked as a dead letter, replayed from the panel")

req = urllib.request.Request(HUB + "/webhooks/shop", b'{"id": 1, "email": "evil@example.com"}',
                             {"X-Shopify-Hmac-Sha256": "forged", "X-Shopify-Topic": "customers/create", "Content-Type": "application/json"})
try:
    urllib.request.urlopen(req)
    raise SystemExit("FAIL: forged webhook accepted")
except urllib.error.HTTPError as e:
    assert e.code == 401
admin("/crm/_admin/expire_tokens", {})
admin(f"/crm/_admin/contacts/{cid}", {"firstname": "Ana Maria"})
wait(lambda: customer("ana.silva@example.com")["first_name"] == "Ana Maria", "the edit after token expiry")
assert crm()["stats"]["refreshes"] >= 1
print("PASS security: forged webhook rejected, expired token refreshed without losing the change")
print("PASS: sync hub end to end")
PY
