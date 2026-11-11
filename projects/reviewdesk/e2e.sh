#!/usr/bin/env bash
# End-to-end in Docker: eight reviews on two locations fetched with paging; positive ones scheduled and posted after
# the review window; low ratings and a food-poisoning claim held for approval with Slack alerts; a draft edited and
# posted from the panel; a review answered in the Google app left alone; a review edited after our reply flagged.
set -euo pipefail
cd "$(dirname "$0")"
./setup.sh >/dev/null
docker compose down -v >/dev/null 2>&1 || true
POLL_SECONDS=1 SYNC_MINUTES=0.02 AUTO_DELAY_MINUTES=0.08 docker compose up -d --build --wait --wait-timeout 300
trap '[ "${KEEP:-1}" = 0 ] && docker compose down -v >/dev/null 2>&1 || true' EXIT
set -a; . ./.env; set +a

python3 - <<'PY'
import json, os, re, time, urllib.parse, urllib.request
from http.cookiejar import CookieJar

APP, APIS = "http://127.0.0.1:8116", "http://127.0.0.1:8117"
H = {"x-admin": os.environ["FAKES_ADMIN_TOKEN"], "Content-Type": "application/json"}
admin = lambda path, body=None: json.loads(urllib.request.urlopen(urllib.request.Request(APIS + path, json.dumps(body).encode() if body is not None else None, H)).read())
web = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
get = lambda url: web.open(url).read().decode()
def post(url, page, **data):
    token = re.search(r'name="csrf" value="([^"]+)"', get(page)).group(1)
    return web.open(url, urllib.parse.urlencode({**data, "csrf": token}).encode()).read().decode()
def wait(cond, what, tries=60):
    for _ in range(tries):
        if cond():
            return
        time.sleep(0.5)
    raise SystemExit(f"FAIL: timed out waiting for {what}")
replies = lambda: {k: v["reviewReply"]["comment"] for k, v in admin("/_admin/state")["reviews"].items() if v.get("reviewReply")}

post(APP + "/login", APP + "/login", email="manager@example.com", password="password")
wait(lambda: get(APP + "/?status=awaiting_approval").count("data-review=") == 4, "the first sync")
page = get(APP + "/?status=awaiting_approval")
assert page.index('data-review="r006"') < page.index('data-review="r004"') and "health: answer today" in page
slack = admin("/_admin/state")["slack"]
assert len(slack) == 2 and any("health issue" in s["text"] for s in slack)
print("PASS sync: 8 reviews on 2 locations; 4 held for approval (the food-poisoning claim first), 2 Slack alerts")

wait(lambda: len(replies()) == 4, "the scheduled replies")
r = replies()
assert r["r001"].startswith("Hi Jonas,") and r["r002"].startswith("Hallo Anna,") and r["r005"].startswith("Hallo,")
assert "r003" not in r  # 4 stars with a "but": a person decides
print("PASS auto replies: 4 positive reviews answered after the review window, in English and German, by first name")

post(APP + "/reviews/r004", APP + "/?status=awaiting_approval", action="post",
     text="Hallo Lukas, das tut mir leid. Die Suppe war an dem Abend versalzen, das hat unsere Küche bestätigt. Melden Sie sich gern bei hello@kiku.example. Mara")
assert "versalzen" in replies()["r004"]
r006 = re.search(r'data-review="r006".*?<textarea name="text">(.*?)</textarea>', get(APP + "/?status=awaiting_approval"), re.S).group(1)
assert "poisoning" not in r006 and "contact me directly" in r006
print("PASS approval: an edited reply posted from the panel; the food-poisoning draft takes it private and doesn't repeat the claim")

admin("/_admin/review", {"author": "Eva", "stars": 5, "text": "Lovely!", "owner_reply": "Thanks Eva!"})

wait(lambda: 'data-review="r009"' in get(APP + "/?status=replied_elsewhere"), "the review answered elsewhere")
assert replies()["r009"] == "Thanks Eva!"
print("PASS respect: a review answered in the Google app was left alone")

stats = get(APP + "/stats")
assert "data-location=\"locations/1001\"" in stats and "Complaints" in stats
print("PASS stats: rating, reply rate, time to answer and complaint topics per location")
PY
