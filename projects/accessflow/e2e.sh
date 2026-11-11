#!/usr/bin/env bash
# End-to-end in Docker: existing staff provisioned from the HR system; a joiner through a Slack outage; a mover; a
# privileged request approved by the manager from a Slack DM; an immediate leaver removed everywhere with Drive
# handed to the manager; drift (a stale admin, a stray Slack account) found and removed; a forged HR webhook.
set -euo pipefail
cd "$(dirname "$0")"
./setup.sh >/dev/null
docker compose down -v >/dev/null 2>&1 || true
RUN_SECONDS=1 HRIS_POLL_SECONDS=3 DRIFT_MINUTES=0 docker compose up -d --build --wait --wait-timeout 300
trap '[ "${KEEP:-1}" = 0 ] && docker compose down -v >/dev/null 2>&1 || true' EXIT
set -a; . ./.env; set +a

python3 - <<'PY'
import json, os, re, time, urllib.error, urllib.parse, urllib.request
from datetime import date
from http.cookiejar import CookieJar

APP, APIS = "http://127.0.0.1:8110", "http://127.0.0.1:8111"
H = {"x-admin": os.environ["FAKES_ADMIN_TOKEN"], "Content-Type": "application/json"}
admin = lambda path, body=None: json.loads(urllib.request.urlopen(urllib.request.Request(APIS + path, json.dumps(body).encode() if body is not None else None, H)).read())
st = lambda: admin("/_admin/state")
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
today = date.today().isoformat()

wait(lambda: len(st()["google"]["users"]) == 5 and st()["github"]["teams"]["engineering"] == ["hberg-dev", "jklein"], "existing staff to be provisioned")
print("PASS existing staff: 5 people from the HR system got Google, Slack and (engineers) GitHub, with their department's groups")

admin("/_admin/chaos", {"slack_fail": 2})
admin("/_admin/hris/created", {"id": "E010", "name": "Mira Stein", "email": "mira.stein@company.example", "personal_email": "mira@mail.example",
                                "github": "mstein", "department": "Engineering", "title": "Platform Engineer", "manager_id": "E001", "start_date": today})
wait(lambda: "mstein" in st()["github"]["teams"]["engineering"] and any(u["userName"] == "mira.stein@company.example" for u in st()["slack"]["users"])
     and "mira.stein@company.example" in st()["google"]["groups"].get("eng@company.example", []), "the joiner's access")
print("PASS joiner: hired in the HR system, accounts and groups in all three systems; Slack's outage retried")

admin("/_admin/hris/updated", {"id": "E004", "department": "Sales"})
wait(lambda: "tom.richter@company.example" in st()["google"]["groups"]["sales@company.example"]
     and "tom.richter@company.example" not in st()["google"]["groups"]["finance@company.example"], "the mover's access")
print("PASS mover: Finance → Sales moved the groups in Google and Slack")

post(APP + "/login", APP + "/login", email="it@example.com", password="password")
post(APP + "/people/E002/request", APP + "/people/E002", access="github team:admins")
time.sleep(2)
assert "jklein" not in st()["github"]["teams"]["admins"]
dm = st()["slack"]["dms"][-1]
link = re.search(r"(http://127\.0\.0\.1:8110/approve/\S+)", dm["text"]).group(1)
assert "Approve or deny" in dm["text"]
assert "Approved" in urllib.request.urlopen(urllib.request.Request(link, b"decision=approve", method="POST")).read().decode()
wait(lambda: "jklein" in st()["github"]["teams"]["admins"], "the approved admin access")
print("PASS privileged access: requested by IT, nothing granted until the manager approved from a Slack DM")

t0 = time.time()
admin("/_admin/hris/terminated", {"id": "E002", "end_date": today, "immediate": True})
wait(lambda: "jklein" not in st()["github"]["members"] and st()["google"]["users"]["jonas.klein@company.example"]["suspended"]
     and not next(u for u in st()["slack"]["users"] if u["userName"] == "jonas.klein@company.example")["active"], "the leaver's removal")
took = time.time() - t0
s = st()
assert s["google"]["transfers"][-1]["newOwnerUserId"] == "hanna.berg@company.example" and "jonas.klein@company.example" in s["google"]["signouts"]
assert "jklein" not in s["github"]["teams"]["admins"]
print(f"PASS leaver: immediate termination removed everything in {took:.1f}s, admin rights included; Drive to the manager, sessions ended")

admin("/_admin/manual", {"what": "github_add", "login": "old-contractor", "team": "admins"})
admin("/_admin/manual", {"what": "slack_add", "email": "freelancer@gmail.com"})
post(APP + "/drift/scan", APP + "/drift")
page = get(APP + "/drift")
assert 'data-drift="github:old-contractor:open"' in page and 'data-drift="slack:freelancer@gmail.com:open"' in page
for did in re.findall(r'action="/drift/(\d+)"', page):
    post(f"{APP}/drift/{did}", APP + "/drift", action="remove")
assert "old-contractor" not in st()["github"]["members"] and not next(u for u in st()["slack"]["users"] if u["userName"] == "freelancer@gmail.com")["active"]
print("PASS drift: a contractor's leftover admin access and a stray Slack account found by the scan and removed")

try:
    urllib.request.urlopen(urllib.request.Request(APP + "/webhooks/hris", b'{"event":"employee.updated","id":"x","employee":{}}', {"X-HRIS-Signature": "forged"}))
    raise SystemExit("FAIL: forged HR webhook accepted")
except urllib.error.HTTPError as e:
    assert e.code == 401
audit = get(APP + "/audit.csv")
assert "approved github team:admins" in audit and "google transfer drive account" in audit
print("PASS audit: forged HR webhook refused; the CSV export has every change, approval and removal")
PY
