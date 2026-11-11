#!/usr/bin/env bash
# End-to-end in Docker: Meta's webhook handshake, a customer tracking an order and registering a return entirely by
# tapping buttons, a duplicate webhook delivery, a handoff to a person who answers from the inbox, the 24-hour rule,
# opt-in shipping updates as templates, and a forged webhook.
set -euo pipefail
cd "$(dirname "$0")"
./setup.sh >/dev/null
docker compose down -v >/dev/null 2>&1 || true
docker compose up -d --build --wait --wait-timeout 300
trap '[ "${KEEP:-1}" = 0 ] && docker compose down -v >/dev/null 2>&1 || true' EXIT
set -a; . ./.env; set +a

python3 - <<'PY'
import json, os, re, time, urllib.error, urllib.parse, urllib.request
from http.cookiejar import CookieJar

APP, APIS = "http://127.0.0.1:8108", "http://127.0.0.1:8109"
LENA, MAX, NEW = "4917612345678", "4917699887766", "4915112223334"
H = {"x-admin": os.environ["FAKES_ADMIN_TOKEN"], "Content-Type": "application/json"}
def admin(path, body=None, method=None):
    return json.loads(urllib.request.urlopen(urllib.request.Request(APIS + path, json.dumps(body).encode() if body is not None else None, H, method=method)).read())
say = lambda who, text, **kw: admin("/_admin/say", {"from": who, "name": kw.get("name", "Lena"), "text": text, **kw})
tap = lambda who, rid, title="", lst=False: admin("/_admin/tap", {"from": who, "id": rid, "title": title, "list": lst})
chat = lambda who: admin(f"/_admin/chat/{who}")
def last(who):
    m = chat(who)[-1]["msg"]
    return m["text"]["body"] if m["type"] == "text" else (m["interactive"]["body"]["text"] if m["type"] == "interactive" else m["template"]["name"])
web = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
get = lambda url: web.open(url).read().decode()
def post(url, page, **data):
    token = re.search(r'name="csrf" value="([^"]+)"', get(page)).group(1)
    return web.open(url, urllib.parse.urlencode({**data, "csrf": token}).encode()).read().decode()

challenge = urllib.request.urlopen(APP + "/webhooks/whatsapp?" + urllib.parse.urlencode(
    {"hub.mode": "subscribe", "hub.verify_token": os.environ["WA_VERIFY_TOKEN"], "hub.challenge": "1158201444"})).read().decode()
assert challenge == "1158201444"
print("PASS handshake: Meta's webhook verification answered with the challenge")

r = say(LENA, "Hi, where is my order?")
assert r["status"] == 200 and "Which order" in last(LENA)
tap(LENA, "track:1042", "Order 1042", True)
assert "on its way with DHL" in last(LENA)
print("PASS tracking: free text understood, the customer's two orders offered as a list, tracking link sent")

tap(LENA, "return", "Return an item")
tap(LENA, "ret:1031", "Order 1031", True)
tap(LENA, "item:SNK-42", "Runner sneakers", True)
r = tap(LENA, "size", "Wrong size")
assert "R-1031-1" in last(LENA) and "label" in last(LENA)
admin("/_admin/resend", {"from": LENA, "id": r["id"]})  # WhatsApp delivers the same tap again
time.sleep(0.5)
assert len(admin("/_admin/returns")) == 1 and "R-1031-2" not in json.dumps(chat(LENA))
print("PASS returns: order, item and reason picked by tapping; return registered with a label; the redelivered webhook was ignored")

post(APP + "/login", APP + "/login", email="care@example.com", password="password")
say(MAX, "I want to complain about the kettle", name="Max")
assert "passed this to our team" in last(MAX)
n = len(chat(MAX))
say(MAX, "Hello??", name="Max")
assert len(chat(MAX)) == n + 1  # the bot stays quiet while a person has it
inbox = get(APP + "/")
cid = re.search(rf'href="/c/(\d+)" data-conv="{MAX}" data-mode="human"', inbox).group(1)
post(f"{APP}/c/{cid}/reply", f"{APP}/c/{cid}", body="Hi Max, Cleo here. Sorry about that, I'll send a replacement today.")
assert last(MAX).startswith("Hi Max, Cleo here")
time.sleep(0.5)
assert 'data-status="read"' in get(f"{APP}/c/{cid}")
print("PASS handoff: complaint passed to a person, bot silent meanwhile, the agent's reply delivered and read")

say(NEW, "hi", name="Nora", age_hours=30)
cid = re.search(rf'href="/c/(\d+)" data-conv="{NEW}"', get(APP + "/?view=all")).group(1)
page = get(f"{APP}/c/{cid}")
assert "only allows approved templates" in page and "hasn't opted in" in page
print("PASS 24-hour rule: 30 hours after the customer's message the inbox offers templates only")

assert admin("/_admin/ship", {"number": "1050"})["status"] == 200
assert last(MAX) != "order_shipped"  # Max never opted in: no update
say(MAX, "START", name="Max")
admin("/_admin/ship", {"number": "1050"})
assert last(MAX) == "order_shipped"
print("PASS updates: shipping template sent only after the customer opted in with START")

body = json.dumps({"entry": []}).encode()
try:
    urllib.request.urlopen(urllib.request.Request(APP + "/webhooks/whatsapp", body, {"X-Hub-Signature-256": "sha256=forged", "Content-Type": "application/json"}))
    raise SystemExit("FAIL: forged webhook accepted")
except urllib.error.HTTPError as e:
    assert e.code == 401
print("PASS security: a forged webhook was refused")
PY
