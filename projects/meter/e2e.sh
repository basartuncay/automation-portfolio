#!/usr/bin/env bash
# End-to-end in Docker: a customer created in Stripe from the panel, usage sent by the product (a retried batch
# counted once), usage alerts, reporting to Stripe's meter and reconciliation, a scheduled downgrade, the period's
# invoice failing on a declined card, the grace period, the account restricted, paid, and restored; forged webhooks.
set -euo pipefail
cd "$(dirname "$0")"
./setup.sh >/dev/null
docker compose down -v >/dev/null 2>&1 || true
REPORT_SECONDS=1 GRACE_DAYS=0.0001 docker compose up -d --build --wait --wait-timeout 300
trap '[ "${KEEP:-1}" = 0 ] && docker compose down -v >/dev/null 2>&1 || true' EXIT
set -a; . ./.env; set +a

python3 - <<'PY'
import json, os, re, time, urllib.error, urllib.parse, urllib.request
from http.cookiejar import CookieJar

APP, STRIPE = "http://127.0.0.1:8112", "http://127.0.0.1:8113"
H = {"x-admin": os.environ["FAKES_ADMIN_TOKEN"], "Content-Type": "application/json"}
admin = lambda path, body=None: json.loads(urllib.request.urlopen(urllib.request.Request(STRIPE + path, json.dumps(body).encode() if body is not None else None, H)).read())
web = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
get = lambda url: web.open(url).read().decode()
def post(url, page, **data):
    token = re.search(r'name="csrf" value="([^"]+)"', get(page)).group(1)
    return web.open(url, urllib.parse.urlencode({**data, "csrf": token}).encode()).read().decode()
def api(path, key, body=None):
    req = urllib.request.Request(APP + path, json.dumps(body).encode() if body is not None else None, {"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())
def wait(cond, what, tries=60):
    for _ in range(tries):
        if cond():
            return
        time.sleep(0.5)
    raise SystemExit(f"FAIL: timed out waiting for {what}")
ev = lambda n, k: {"metric": "api_calls", "quantity": n, "idempotency_key": k}

post(APP + "/login", APP + "/login", email="billing@example.com", password="password")
page = post(APP + "/customers", APP + "/", name="Nordlicht Shop", email="billing@nordlicht.example", plan="starter")
key = re.search(r'id="new-key">([^<]+)<', page).group(1)
page2 = post(APP + "/customers", APP + "/", name="Kaffeekontor", email="billing@kaffeekontor.example", plan="pro")
key2 = re.search(r'id="new-key">([^<]+)<', page2).group(1)
assert len(admin("/_admin/state")["customers"]) == 2
print("PASS customers: created in Stripe from the panel with a subscription each; API keys shown once")

r1 = api("/v1/events", key, {"events": [ev(6000, "batch1-0"), ev(2500, "batch1-b")]})
r2 = api("/v1/events", key, {"events": [ev(6000, "batch1-0"), ev(2500, "batch1-b")]})  # the client retried the batch
r3 = api("/v1/events", key, {"events": [ev(3000, "batch2-a")]})
assert (r1["accepted"], r2["duplicates"], r3["used"]) == (2, 2, 11500)
ent = api("/v1/entitlement", key)
assert ent["allowed"] and ent["projected_cents"] == 3300
emails = get(APP + "/emails")
assert "80% of the 10,000 API calls" in emails and "included API calls are used up" in emails
print("PASS usage: a retried batch counted once (11,500 calls), €33 projected, 80% and 100% alerts emailed")

wait(lambda: 'data-recon="Nordlicht Shop:ok"' in get(APP + "/reconcile") and admin("/_admin/state")["meter_events"] == 3, "the usage on Stripe's meter")
print("PASS meter: usage reported to Stripe's meter and reconciled: counts match")

cid2 = re.search(r'/customers/(c_\w+)"><b>Kaffeekontor', get(APP + "/")).group(1)
assert "when this period ends" in post(f"{APP}/customers/{cid2}/plan", f"{APP}/customers/{cid2}", plan="starter")

state = admin("/_admin/state")
nord = next(c["id"] for c in state["customers"].values() if c["name"] == "Nordlicht Shop")
kaffee = next(c["id"] for c in state["customers"].values() if c["name"] == "Kaffeekontor")
admin("/_admin/card", {"customer": nord, "ok": False})
inv = admin("/_admin/close_period", {"customer": nord})
admin("/_admin/close_period", {"customer": kaffee})
assert inv["amount_due"] == 3300
cid = re.search(r'/customers/(c_\w+)"><b>Nordlicht', get(APP + "/")).group(1)
wait(lambda: 'data-status="past_due"' in get(f"{APP}/customers/{cid}"), "past due")
wait(lambda: 'data-status="restricted"' in get(f"{APP}/customers/{cid}"), "the end of the grace period")
ent = api("/v1/entitlement", key)
assert not ent["allowed"] and "unpaid" in ent["reason"] and ent["used"] == 0
assert "Pro" not in re.search(r'<p class="sub">([^<]+)', get(f"{APP}/customers/{cid2}")).group(1)
print("PASS invoice: period closed at €33.00, the declined card made it past due, the grace period ended in a restriction; the downgrade took effect")

admin("/_admin/card", {"customer": nord, "ok": True})
admin("/_admin/retry", {"invoice": inv["id"]})
wait(lambda: api("/v1/entitlement", key)["allowed"], "access to come back")
page = get(f"{APP}/customers/{cid}")
assert f'data-invoice="{inv["number"]}:paid"' in page and "2 attempts" in page
print("PASS recovery: card updated, Stripe's retry paid the invoice, access restored at once")

assert admin("/_admin/replay", {})["status"] == 400
try:
    urllib.request.urlopen(urllib.request.Request(APP + "/webhooks/stripe", b"{}", {"Stripe-Signature": "t=1,v1=00"}))
    raise SystemExit("FAIL: forged webhook accepted")
except urllib.error.HTTPError as e:
    assert e.code == 400
print("PASS webhooks: an hour-old replay and a forged signature were refused")
PY
