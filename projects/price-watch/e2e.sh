#!/usr/bin/env bash
# End-to-end: start the stack, let the first crawl baseline both competitors, change competitor prices, press
# "Crawl now" in the panel, and check alerts (panel + Slack webhook), the price history and the emailed digest.
# Needs Docker and python3.
set -euo pipefail
cd "$(dirname "$0")"
./setup.sh >/dev/null
docker compose up -d --build --wait --wait-timeout 300
trap '[ "${KEEP:-1}" = 0 ] && docker compose down -v >/dev/null 2>&1 || true' EXIT
set -a; . ./.env; set +a

python3 - <<'PY'
import email, imaplib, json, os, re, time, urllib.parse, urllib.request
from email import policy
from http.cookiejar import CookieJar

BASE, SHOP = "http://127.0.0.1:8091", "http://127.0.0.1:8092"
web = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
get = lambda path: web.open(BASE + path).read().decode()
def post(path, form_page, **data):
    token = re.search(r'name="csrf" value="([^"]+)"', get(form_page)).group(1)
    return web.open(BASE + path, urllib.parse.urlencode(data | {"csrf": token}).encode())
def shop(path, body):
    req = urllib.request.Request(SHOP + path, json.dumps(body).encode(), {"Content-Type": "application/json",
                                 "x-token": os.environ["SHOP_ADMIN_TOKEN"]})
    return urllib.request.urlopen(req).read()
def wait_for(cond, what, tries=60):
    for _ in range(tries):
        if cond():
            return
        time.sleep(1)
    raise SystemExit(f"FAIL: timed out waiting for {what}")

post("/login", "/login", email="pricing@example.com", password="password")
wait_for(lambda: get("/runs").count("chip ok\">") >= 2, "the baseline crawl")
print("baseline crawl done:", get("/").count('data-pos="cheapest"'), "of 12 products cheapest")

shop("/voltmart/admin/set", {"pid": "wireless-headphones-x200", "price": 119.00})   # undercut
shop("/gadgethub/admin/set", {"pid": "usb-c-charger-65w", "in_stock": False})        # stock
post("/request/crawl", "/")
wait_for(lambda: "undercuts you" in get("/alerts"), "the undercut alert")
page = get("/")
assert page.count('data-pos="undercut"') == 1, "exactly one product should be undercut"
alerts = re.findall(r'<td>(VoltMart undercuts you[^<]+|GadgetHub: [^<]+)</td>', get("/alerts"))
print("alerts:", *alerts, sep="\n  ")
assert any("Wireless Headphones X200: €119.00 vs your €128.99" in a for a in alerts)
assert any("USB-C Charger 65W is out of stock" in a for a in alerts)
assert get("/products/SKU-1001").count('<path d="M') == 2

wait_for(lambda: len(json.loads(urllib.request.urlopen(SHOP + "/hooks/slack").read())) >= 2, "Slack delivery")
print("slack received:", len(json.loads(urllib.request.urlopen(SHOP + "/hooks/slack").read())), "messages")

shop("/admin/layout", {"broken": True})  # simulate a competitor redesign
post("/request/crawl", "/")
wait_for(lambda: get("/runs").count("chip suspicious\">") >= 2, "suspicious runs")
assert get("/alerts").count("layout probably changed") == 2, "one scraper alert per competitor"
assert get("/").count('data-pos="undercut"') == 1, "a broken crawl must not change any prices"
shop("/admin/layout", {"broken": False})
print("layout change detected: nothing updated, one scraper alert per competitor")

post("/request/digest", "/")
def digest():
    imap = imaplib.IMAP4("127.0.0.1", 3144)
    try:  # GreenMail creates the mailbox (login = password = address) when the first mail arrives
        imap.login("pricing-team@shop.example", "pricing-team@shop.example")
    except imaplib.IMAP4.error:
        return None
    imap.select("INBOX")
    ids = imap.search(None, "ALL")[1][0].split()
    return email.message_from_bytes(imap.fetch(ids[-1], "(RFC822)")[1][0][1], policy=policy.default) if ids else None
wait_for(lambda: digest() is not None, "the digest email")
msg = digest()
html = msg.get_body(("html",)).get_content()
assert msg["Subject"] == "Price Watch weekly: 1 product(s) undercut" and "Wireless Headphones X200" in html
print("digest received:", msg["Subject"])
print("PASS: baseline silent, undercut + stock alerts in panel and Slack, redesign caught without false changes, digest emailed")
PY
