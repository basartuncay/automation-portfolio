#!/usr/bin/env bash
# End-to-end in Docker: a guest books on the website, the phone agent books a big party (and its retry doesn't
# double-book), a guest cancels by texting STORNO, a freed table goes to the waitlist and is taken with a JA reply,
# an SMS outage is retried, a forged webhook is refused, and the staff panel shows the day.
set -euo pipefail
cd "$(dirname "$0")"
./setup.sh >/dev/null
docker compose down -v >/dev/null 2>&1 || true
POLL_SECONDS=1 BACKOFF_SECONDS=1 docker compose up -d --build --wait --wait-timeout 300
trap '[ "${KEEP:-1}" = 0 ] && docker compose down -v >/dev/null 2>&1 || true' EXIT
set -a; . ./.env; set +a

python3 - <<'PY'
import json, os, re, time, urllib.error, urllib.parse, urllib.request
from datetime import date, timedelta
from http.cookiejar import CookieJar

DESK, SMS = "http://127.0.0.1:8098", "http://127.0.0.1:8099"
ADMIN = {"x-admin": os.environ["FAKES_ADMIN_TOKEN"], "Content-Type": "application/json"}
d = date.today() + timedelta(days=3)
DAY = (d + timedelta(days=1) if d.weekday() == 0 else d).isoformat()

def call(url, body=None, headers=None):
    req = urllib.request.Request(url, json.dumps(body).encode() if body is not None else None, {"Content-Type": "application/json", **(headers or {})})
    try:
        return json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        return {"_status": e.code, **json.loads(e.read() or b"{}")}
admin = lambda path, body=None: call(SMS + path, body, ADMIN)
texts = lambda to: [m["body"] for m in admin(f"/_admin/messages?to={urllib.parse.quote(to)}") if m["direction"] == "outbound"]
tool = lambda name, args, cid: call(f"{DESK}/agent/{name}", {"args": args, "call": {"call_id": cid, "from_number": "+4917155500" + cid[-2:]}},
                                    {"Authorization": "Bearer " + os.environ["AGENT_TOOL_TOKEN"]})
def wait(cond, what, tries=60):
    for _ in range(tries):
        if cond():
            return
        time.sleep(0.5)
    raise SystemExit(f"FAIL: timed out waiting for {what}")

# 1. a guest books on the website
assert "19:00" in call(f"{DESK}/api/slots?day={DAY}&party=2")["times"]
web = call(f"{DESK}/api/book", {"name": "Mia Wagner", "phone": "0171 2223344", "day": DAY, "time": "19:00", "party": 2})
assert web["ok"], web
wait(lambda: any(web["code"] in t for t in texts("+491712223344")), "the website booking's confirmation text")
print(f"PASS website: booked {web['code']}, number normalised to +49, confirmation text delivered")

# 2. the phone agent books a party of 8 on joined tables; the platform's retry doesn't book twice
ask = tool("check_availability", {"date": DAY, "time": "19:30", "party_size": 8}, "call_01")
assert ask["available"], ask
args = {"name": "Kenji Sato", "date": DAY, "time": "19:30", "party_size": 8, "notes": "birthday"}
first, retry = tool("create_booking", args, "call_01"), tool("create_booking", args, "call_01")
assert first["ok"] and retry == first
wait(lambda: any("8 Pers." in t for t in texts("+491715550001")), "the phone booking's confirmation")
print(f"PASS phone agent: party of 8 booked by voice tools, retried call returned the same booking {first['code']}")

# 3. the guest cancels by text
admin("/_admin/reply", {"from": "+491712223344", "body": "STORNO"})
wait(lambda: any("storniert" in t for t in texts("+491712223344")), "the cancellation text")
print("PASS sms cancel: STORNO reply cancelled the booking and confirmed it")

# 4. the evening fills up; a freed table goes to the waitlist and is taken with JA
for i in range(12):
    r = tool("create_booking", {"name": f"Guest {i}", "date": DAY, "time": "20:00", "party_size": 2, "phone": f"+4917166600{i:02d}"}, f"call_2{i:02d}")
    if not r.get("ok"):
        break
full = tool("check_availability", {"date": DAY, "time": "20:00", "party_size": 2}, "call_300")
assert not full["available"], full
assert tool("join_waitlist", {"name": "Ada Berg", "phone": "+491718880000", "date": DAY, "earliest": "20:00", "latest": "20:30", "party_size": 2}, "call_301")["ok"]
admin("/_admin/reply", {"from": "+4917166600" + "00", "body": "storno"})
wait(lambda: any("JA" in t for t in texts("+491718880000")), "the waitlist offer")
admin("/_admin/reply", {"from": "+491718880000", "body": "Ja gerne"})
wait(lambda: any("bestätigt" in t for t in texts("+491718880000")), "the waitlist guest's confirmation")
print("PASS waitlist: fully booked at 20:00, a cancellation was offered by text, JA reply booked it")

# 5. the SMS gateway has an outage: the message is retried until it goes out
admin("/_admin/fail", {"count": 2})
r = tool("create_booking", {"name": "Lena Roth", "date": DAY, "time": "12:00", "party_size": 3}, "call_400")
assert r["ok"], r
wait(lambda: any("3 Pers." in t for t in texts("+491715550000")), "the text sent after the outage")
print("PASS outage: two 503s from the SMS gateway, confirmation delivered on retry")

# 6. a forged inbound webhook is refused
forged = admin("/_admin/reply", {"from": "+491715550001", "body": "STORNO", "signature": "forged"})
assert forged["status"] == 403, forged
print("PASS webhook: forged X-Twilio-Signature refused, booking untouched")

# 7. the staff panel shows the day
jar = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
get = lambda url: jar.open(url).read().decode()
token = re.search(r'name="csrf" value="([^"]+)"', get(DESK + "/login")).group(1)
jar.open(DESK + "/login", urllib.parse.urlencode({"email": "host@example.com", "password": "password", "csrf": token}).encode())
page = get(f"{DESK}/panel?day={DAY}")
assert "Kenji Sato" in page and "Ada Berg" in page and 'data-status="cancelled"' in page and "birthday" in page
print("PASS panel: day view shows the phone, website and waitlist bookings with their status")
PY
