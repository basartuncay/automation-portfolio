#!/usr/bin/env bash
# End-to-end in Docker: tasks in through the API and the panel, a refund held for approval and approved by a manager,
# the shop data checked, the eval suite run inside the container, and an MCP client hitting the same policy.
# Forces the offline planner so the result is deterministic and free.
set -euo pipefail
cd "$(dirname "$0")"
export ANTHROPIC_API_KEY=""
./setup.sh >/dev/null
docker compose down -v >/dev/null 2>&1 || true  # start from an empty shop
docker compose up -d --build --wait --wait-timeout 300
trap '[ "${KEEP:-1}" = 0 ] && docker compose down -v >/dev/null 2>&1 || true' EXIT
set -a; . ./.env; set +a

python3 - <<'PY'
import json, os, re, time, urllib.parse, urllib.request
from http.cookiejar import CookieJar

BASE = "http://127.0.0.1:8093"
AUTH = {"Authorization": "Bearer " + os.environ["API_TOKEN"], "Content-Type": "application/json"}
def api(path, body=None):
    req = urllib.request.Request(BASE + path, json.dumps(body).encode() if body else None, AUTH)
    return json.loads(urllib.request.urlopen(req).read())
def wait_run(rid, want):
    for _ in range(60):
        r = api(f"/api/runs/{rid}")
        if r["status"] == want:
            return r
        time.sleep(0.5)
    raise SystemExit(f"FAIL: run {rid} is {r['status']}, expected {want}")

small = api("/api/tasks", {"task": "Refund €19.99 on order 1004 because the box arrived dented."})["run_id"]
big = api("/api/tasks", {"task": "Refund €120 on order 1012 because both items arrived broken."})["run_id"]
print("small refund:", wait_run(small, "done")["summary"])
print("big refund:", wait_run(big, "waiting_approval")["status"])

web = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
get = lambda p: web.open(BASE + p).read().decode()
def post(p, page, **data):
    token = re.search(r'name="csrf" value="([^"]+)"', get(page)).group(1)
    return web.open(BASE + p, urllib.parse.urlencode(data | {"csrf": token}).encode())
post("/login", "/login", email="manager@example.com", password="password")
aid = re.search(r'action="/approvals/(\d+)"', get("/approvals")).group(1)
post(f"/approvals/{aid}", "/approvals", decision="approve")
print("after approval:", wait_run(big, "done")["summary"])
shop = get("/shop")
assert "€19.99" in shop and "€120.00" in shop and "approved by Mia Manager" in shop
trace = get(f"/runs/{big}")
assert trace.count('class="trace"') == 1 and "above the €50.00 automatic limit" in trace and "Mia Manager" in trace
print("PASS api+panel: small refund automatic, large refund held, approved, executed, traced")
PY

docker compose exec -T web python -m ops_agent.evals | tail -3
docker compose exec -T web python - <<'PY'
import asyncio, os, sys
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
async def main():
    async with stdio_client(StdioServerParameters(command=sys.executable, args=["-m", "ops_agent.mcp_server"], env=dict(os.environ))) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        print("mcp tools:", len((await s.list_tools()).tools))
        res = await s.call_tool("issue_refund", {"order_id": 1012, "amount": 999, "reason": "x"})
        assert res.is_error and "left to refund" in res.content[0].text, res
        res = await s.call_tool("cancel_order", {"order_id": 1005, "reason": "duplicate"})
        assert not res.is_error and "must approve" in res.content[0].text, res
        print("PASS mcp: over-refund refused, cancellation queued for a manager")
asyncio.run(main())
PY
curl -sf -o /dev/null http://127.0.0.1:8093/healthz && echo "PASS: ops agent stack"
