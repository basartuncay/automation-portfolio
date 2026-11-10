#!/usr/bin/env bash
# End-to-end: start the stack, email 6 customer messages to the support inbox, check how each was handled,
# send one AI draft through the panel as an agent, and read the reply from the customer's mailbox.
# Needs Docker and python3. Forces the offline engine so the result is deterministic and free.
set -euo pipefail
cd "$(dirname "$0")"
export ANTHROPIC_API_KEY=""
./setup.sh >/dev/null
docker compose up -d --build --wait --wait-timeout 300
trap '[ "${KEEP:-1}" = 0 ] && docker compose down -v >/dev/null 2>&1 || true' EXIT

python3 - <<'PY'
import imaplib, json, re, smtplib, time, urllib.parse, urllib.request, email
from email import policy
from http.cookiejar import CookieJar
from pathlib import Path

BASE = "http://127.0.0.1:8090"
with smtplib.SMTP("127.0.0.1", 3025) as s:
    for p in sorted(Path("samples").glob("*.eml")):
        s.sendmail("customer@example.com", ["support@shop.example"], p.read_bytes())
print("sent 6 emails")

web = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
def get(path):
    return web.open(BASE + path).read().decode()
def post(path, form_page, **data):
    token = re.search(r'name="csrf" value="([^"]+)"', get(form_page)).group(1)
    return web.open(BASE + path, urllib.parse.urlencode(data | {"csrf": token}).encode())

post("/login", "/login", email="agent@example.com", password="password")
for _ in range(60):  # the worker polls every 3s
    page = get("/?status=all")
    if page.count('class="row"') >= 6:
        break
    time.sleep(1)
rows = re.findall(r'<tr class="row" onclick="location.href=\'/tickets/(\d+)\'">.*?<a href="/tickets/\d+">([^<]+)</a>.*?data-status="(\w+)"', page, re.S)
got = {subject.strip(): (int(tid), status) for tid, subject, status in rows}
for subject, (tid, status) in sorted(got.items(), key=lambda x: x[1]):
    print(f"#{tid:<3} {status:<12} {subject}")
want = {"Refund for my returned headphones": "drafted", "Express shipping - need it today": "drafted",
        "Can I pay with PayPal?": "needs_human", "Order 5521": "needs_human",
        "SEO services for your store": "closed", "Invoice NP/7790": "routed"}
assert {k: v[1] for k, v in got.items()} == want, got

tid = got["Refund for my returned headphones"][0]
ticket = get(f"/tickets/{tid}")
draft = re.search(r'<textarea name="reply"[^>]*>(.*?)</textarea>', ticket, re.S).group(1)
assert "within 5 business days" in draft and "How refunds are paid" in ticket
post(f"/tickets/{tid}/send", f"/tickets/{tid}", reply=draft.replace("&#39;", "'"))
assert "Reply sent" in get(f"/tickets/{tid}")

urgent = got["Express shipping - need it today"][0]
assert "need a lead" in get(f"/tickets/{urgent}"), "an agent must not be able to send an urgent reply"

for _ in range(20):
    imap = imaplib.IMAP4("127.0.0.1", 3143)
    imap.login("maria.lopez@customer.example", "maria.lopez@customer.example")  # GreenMail auto-created mailbox
    imap.select("INBOX")
    ids = imap.search(None, "ALL")[1][0].split()
    if ids:
        break
    imap.logout()
    time.sleep(1)
msg = email.message_from_bytes(imap.fetch(ids[-1], "(RFC822)")[1][0][1], policy=policy.default)
assert msg["Subject"] == "Re: Refund for my returned headphones" and msg["In-Reply-To"] == "<r1@customer.example>"
assert "within 5 business days" in msg.get_content()
print(f"customer received: {msg['Subject']!r} in reply to {msg['In-Reply-To']}")
print("PASS: 2 drafted, 2 held for a person (unanswered + injection), spam closed, invoice routed; reply delivered in thread")
PY
docker compose logs worker --no-log-prefix | grep -E "engine:|->" | tail -8
