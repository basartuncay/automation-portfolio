#!/usr/bin/env bash
# End-to-end in Docker: today's CAMT.053 statement and an overlapping CSV export are imported through the panel,
# payments are matched (exact, early-payment discount, bank fee, two invoices in one transfer, part payment) and
# booked in the accounting system through an outage, a person settles the two unclear ones, and reminders go to
# the customers who are really overdue, by email.
set -euo pipefail
cd "$(dirname "$0")"
./setup.sh >/dev/null
docker compose down -v >/dev/null 2>&1 || true
POLL_SECONDS=1 DUNNING_AT=off docker compose up -d --build --wait --wait-timeout 300
trap '[ "${KEEP:-1}" = 0 ] && docker compose down -v >/dev/null 2>&1 || true' EXIT
uv run --quiet python -m paymatch.demo_data samples/ >/dev/null   # statements dated today
set -a; . ./.env; set +a

python3 - <<'PY'
import email, email.policy, imaplib, json, os, re, time, urllib.parse, urllib.request
from http.cookiejar import CookieJar

APP, APIS = "http://127.0.0.1:8106", "http://127.0.0.1:8107"
H = {"x-admin": os.environ["FAKES_ADMIN_TOKEN"], "Content-Type": "application/json"}
admin = lambda path, body=None: json.loads(urllib.request.urlopen(urllib.request.Request(APIS + path, json.dumps(body).encode() if body is not None else None, H)).read())
web = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
get = lambda url: web.open(url).read().decode()
csrf = lambda page: re.search(r'name="csrf" value="([^"]+)"', get(page)).group(1)
post = lambda url, page, data: web.open(url, urllib.parse.urlencode({**data, "csrf": csrf(page)}, doseq=True).encode()).read().decode()
def upload(path):
    b = "----e2e" + os.urandom(6).hex()
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"csrf\"\r\n\r\n{csrf(APP + '/')}\r\n--{b}\r\n"
            f"Content-Disposition: form-data; name=\"file\"; filename=\"{os.path.basename(path)}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode() \
        + open(path, "rb").read() + f"\r\n--{b}--\r\n".encode()
    r = web.open(urllib.request.Request(APP + "/import", body, {"Content-Type": f"multipart/form-data; boundary={b}"}))
    return urllib.parse.unquote_plus(r.geturl()), r.read().decode()
def inbox(addr):
    m = imaplib.IMAP4("127.0.0.1", 3149)
    try:
        m.login(addr, "x"); m.select("INBOX")
        return [email.message_from_bytes(m.fetch(i, "(RFC822)")[1][0][1], policy=email.policy.default) for i in m.search(None, "ALL")[1][0].split()]
    finally:
        m.logout()
def wait(cond, what, tries=60):
    for _ in range(tries):
        try:
            if cond():
                return
        except (OSError, imaplib.IMAP4.error):
            pass
        time.sleep(1)
    raise SystemExit(f"FAIL: timed out waiting for {what}")

post(APP + "/login", APP + "/login", {"email": "finance@example.com", "password": "password"})
wait(lambda: "RE-2026-0111" in get(APP + "/invoices"), "the open invoices to sync")
admin("/_admin/chaos", {"fail": 2})  # the accounting API is down for a moment while we book
url, page = upload("samples/statement.camt053.xml")
assert "9 new transactions" in url and "6 matched automatically, 2 need a look" in url, url
for kind in ("RE-2026-0101:exact", "RE-2026-0102:skonto", "RE-2026-0103:fee", "RE-2026-0104:multi", "RE-2026-0105:multi", "RE-2026-0106:partial", "RE-2026-0111:exact"):
    assert f'data-kind="{kind}"' in page, kind
print("PASS CAMT.053: 9 entries read, 6 payments matched on their own: exact, 2% discount, bank fee, two invoices in one transfer, part payment")

url, _ = upload("samples/umsaetze.csv")
assert "1 new transactions (2 already imported)" in url and "1 matched automatically" in url, url
print("PASS CSV: the bank's German CSV export overlapped the CAMT file by 2 bookings, imported once; the new payment matched")

wait(lambda: admin("/_admin/state")["inv_102"]["open_cents"] == 0, "bookings in the accounting system")
st = admin("/_admin/state")
assert st["inv_102"]["payments"][0]["writeoff_cents"] == 4760 and st["inv_103"]["payments"][0]["writeoff_cents"] == 750
assert st["inv_106"]["open_cents"] == 60000 and st["inv_104"]["open_cents"] == 0 and st["inv_105"]["open_cents"] == 0
wait(lambda: "data-unbooked" not in get(APP + "/"), "every booking to be confirmed")
print("PASS booking: payments and write-offs (discount, fee) written to the accounting system through 2 outages, each exactly once")

review = get(APP + "/review")
mia = re.search(r'data-tx="(\d+)".*?Mia Wagner', review, re.S).group(1)
block = review.split(f'data-tx="{mia}"')[1].split('data-tx=')[0]
pick = re.findall(r'name="pick" value="(\d+)">\s*<div><b>([^<]+)</b>', block)
post(f"{APP}/review/{mia}", APP + "/review", {"pick": next(i for i, n in pick if n == "RE-2026-0107")})
amazon = re.search(r'data-tx="(\d+)".*?AMAZON PAYMENTS', get(APP + "/review"), re.S).group(1)
post(f"{APP}/review/{amazon}", APP + "/review", {"action": "ignore"})
assert "Nothing to review" in get(APP + "/review")
wait(lambda: admin("/_admin/state")["inv_107"]["open_cents"] == 0, "the confirmed payment to be booked")
print("PASS review: €560 from a private name fitted two invoices equally; confirmed by a person, the marketplace payout marked as not an invoice")

page = post(APP + "/dunning", APP + "/invoices", {})
assert "2 reminder(s) queued" in page, re.search(r'alert[^>]*>([^<]+)', page)
wait(lambda: inbox("luca@velo-atelier.ch") and inbox("ap@greenleaf-cosmetics.de"), "the reminders to arrive")
assert inbox("luca@velo-atelier.ch")[0]["Subject"] == "Friendly reminder: invoice RE-2026-0108"
assert "€600.00" in inbox("ap@greenleaf-cosmetics.de")[0].get_content()  # only what's still open after the part payment
for paid in ("buchhaltung@nordlicht-shop.de", "eva@lumen-lighting.nl", "mia@pfoten-glueck.de"):
    assert not inbox(paid), f"{paid} got a reminder for a paid invoice"
assert "No reminders due" in post(APP + "/dunning", APP + "/invoices", {})
print("PASS reminders: only the 2 customers really overdue got one (with the remaining amount); paid customers got none; no second letter the same week")
PY
