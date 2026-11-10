#!/usr/bin/env bash
# End-to-end in Docker: the first scheduled run loads 60 days from three sources and must match the sources' own
# totals to the cent; then real-world failures are injected (negative ad spend, a renamed CSV column) and each must
# fail the run without changing what the dashboard shows; a late refund must change a past day.
set -euo pipefail
cd "$(dirname "$0")"
./setup.sh >/dev/null
docker compose down -v >/dev/null 2>&1 || true
docker compose up -d --build --wait --wait-timeout 300
trap '[ "${KEEP:-1}" = 0 ] && docker compose down -v >/dev/null 2>&1 || true' EXIT
set -a; . ./.env; set +a

python3 - <<'PY'
import json, os, re, subprocess, time, urllib.parse, urllib.request
from http.cookiejar import CookieJar

WEB, SRC = "http://127.0.0.1:8096", "http://127.0.0.1:8097"
ADMIN = {"x-admin": os.environ["SOURCES_ADMIN_TOKEN"], "Content-Type": "application/json"}
web = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
get = lambda p: web.open(WEB + p).read().decode()
def post(p, page, **data):
    token = re.search(r'name="csrf" value="([^"]+)"', get(page)).group(1)
    return web.open(WEB + p, urllib.parse.urlencode(data | {"csrf": token}).encode()).read().decode()
def chaos(**body):
    urllib.request.urlopen(urllib.request.Request(SRC + "/_admin/chaos", json.dumps(body).encode(), ADMIN))
truth = lambda: json.loads(urllib.request.urlopen(urllib.request.Request(SRC + "/_admin/truth", headers=ADMIN)).read())
def served():
    sql = "select sum(orders), round(sum(gross),2), round(sum(refunds),2), round(sum(net),2), round(sum(ad_spend),2) from marts.fct_daily"
    out = subprocess.run(["docker", "compose", "exec", "-T", "web", "python", "-c",
                          f"import duckdb; print(list(map(float, duckdb.connect('/data/serving.duckdb', read_only=True).execute({sql!r}).fetchone())))"],
                         capture_output=True, text=True, check=True).stdout
    o, g, r, n, s = json.loads(out)
    return {"orders": int(o), "gross": g, "refunds": r, "net": n, "ad_spend": s}
def runs():
    return re.findall(r'data-status="(\w+)"', get("/runs"))
def wait_run(n, want):
    for _ in range(120):
        r = runs()
        if len(r) >= n and r[0] != "running" and "Running" not in get("/runs"):
            assert r[0] == want, (r[0], want)
            return
        time.sleep(1)
    raise SystemExit(f"FAIL: run {n} never finished")
expected = lambda: {k: truth()[k] for k in ("orders", "gross", "refunds", "net", "ad_spend")}

post("/login", "/login", email="finance@example.com", password="password")
wait_run(1, "success")
assert served() == expected(), (served(), expected())
assert "All data tests passed" in get("/")
print("PASS first run: 60 days from 3 sources, warehouse equals the sources' own totals:", served())

chaos(negative_spend=True)
good = served()
post("/runs", "/runs")
wait_run(2, "failed")
assert "assert_no_negative_ad_spend" in get("/runs") and served() == good
dash = get("/")
assert "The latest run failed" in dash and "You're seeing data verified at" in dash
chaos(clear_negative_spend=True)
print("PASS bad data: negative spend caught by a dbt test, run not published, dashboard warned and kept verified data")

chaos(renamed_column=True)
post("/runs", "/runs")
wait_run(3, "failed")
assert "missing column(s) [&#39;Cost&#39;]" in get("/runs") and served() == good
chaos(renamed_column=False)
print("PASS schema drift: renamed CSV column stopped the run before dbt, nothing published")

chaos(late_refund=5003)
post("/runs", "/runs")
wait_run(4, "success")
assert served() == expected() and served()["refunds"] > good["refunds"]
assert "All data tests passed" in get("/")
print("PASS late refund: picked up by the overlap window, past day restated, totals match the sources again")
print("PASS: revenue pipeline end to end")
PY
