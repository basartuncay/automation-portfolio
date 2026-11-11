#!/usr/bin/env bash
# End-to-end in Docker with a real mail server: import two exports through the panel (dedupe, suppression), a signed
# form lead, enrichment through a rate limit, scoring, a 3-step sequence into the prospects' real mailboxes, five kinds
# of reply over SMTP/IMAP (interested, out of office, unsubscribe, not interested, bounce), the unsubscribe link, and
# the CRM handoff.
set -euo pipefail
cd "$(dirname "$0")"
./setup.sh >/dev/null
docker compose down -v >/dev/null 2>&1 || true
POLL_SECONDS=1 DAY_SECONDS_SCALE=0.0001 docker compose up -d --build --wait --wait-timeout 300
trap '[ "${KEEP:-1}" = 0 ] && docker compose down -v >/dev/null 2>&1 || true' EXIT
set -a; . ./.env; set +a

python3 - <<'PY'
import email, email.policy, hashlib, hmac, imaplib, json, os, re, smtplib, time, urllib.error, urllib.parse, urllib.request
from email.message import EmailMessage
from http.cookiejar import CookieJar

APP, APIS = "http://127.0.0.1:8100", "http://127.0.0.1:8101"
ADMIN = {"x-admin": os.environ["FAKES_ADMIN_TOKEN"], "Content-Type": "application/json"}
web = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
get = lambda url: web.open(url).read().decode()
csrf = lambda page: re.search(r'name="csrf" value="([^"]+)"', get(page)).group(1)
post = lambda url, page, **data: web.open(url, urllib.parse.urlencode(data | {"csrf": csrf(page)}).encode()).read().decode()
def admin(path, body=None):
    return json.loads(urllib.request.urlopen(urllib.request.Request(APIS + path, json.dumps(body).encode() if body is not None else None, ADMIN)).read())
def upload(path):
    b = "----e2e" + os.urandom(6).hex()
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"csrf\"\r\n\r\n{csrf(APP + '/imports')}\r\n--{b}\r\n"
            f"Content-Disposition: form-data; name=\"file\"; filename=\"{os.path.basename(path)}\"\r\nContent-Type: text/csv\r\n\r\n").encode() \
        + open(path, "rb").read() + f"\r\n--{b}--\r\n".encode()
    return web.open(urllib.request.Request(APP + "/imports", body, {"Content-Type": f"multipart/form-data; boundary={b}"})).read().decode()
def inbox(addr):
    m = imaplib.IMAP4("127.0.0.1", 3145)
    try:
        m.login(addr, "any")
        m.select("INBOX")
        out = []
        for i in m.search(None, "ALL")[1][0].split():
            out.append(email.message_from_bytes(m.fetch(i, "(RFC822)")[1][0][1], policy=email.policy.default))
        return out
    finally:
        m.logout()
def send(msg):
    with smtplib.SMTP("127.0.0.1", 3027) as s:
        s.send_message(msg)
def answer(orig, text, subject=None, **headers):
    m = EmailMessage()
    m["From"], m["To"] = orig["To"], "basar@outreach.example"
    m["Subject"] = subject or "Re: " + orig["Subject"]
    m["Message-ID"], m["In-Reply-To"], m["References"] = f"<{os.urandom(8).hex()}@prospect.example>", orig["Message-ID"], orig["Message-ID"]
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    m.set_content(text + "\n\nOn Wed, Basar Tuncay wrote:\n> Hi")
    send(m)
status = lambda addr: re.search(r'data-status="(\w+)"', get(APP + "/leads?q=" + urllib.parse.quote(addr))).group(1)
def wait(cond, what, tries=120):
    for _ in range(tries):
        try:
            if cond():
                return
        except (OSError, AttributeError, imaplib.IMAP4.error):
            pass
        time.sleep(1)
    raise SystemExit(f"FAIL: timed out waiting for {what}")

post(APP + "/login", APP + "/login", email="growth@example.com", password="password")
admin("/_admin/chaos", {"rate_limit": 2})  # the provider throttles the first calls
page = upload("samples/apollo-export.csv")
assert "20 rows read as <b>apollo</b>: 18 new, 1 merged" in page and "1 invalid" in page, page[:2000]
post(APP + "/setup/suppressions", APP + "/setup", value="competitor.de")
page = upload("samples/salesnav-export.csv")
assert "<b>salesnav</b>: 2 new, 2 merged" in page and "1 on the do-not-contact list" in page
body = json.dumps({"email": "noor@lumen-lighting.nl", "first_name": "Noor", "company": "Lumen Lighting", "title": "Founder"}).encode()
sig = "sha256=" + hmac.new(os.environ["FORM_WEBHOOK_SECRET"].encode(), body, hashlib.sha256).hexdigest()
try:
    urllib.request.urlopen(urllib.request.Request(APP + "/webhooks/form", body, {"X-Signature": "sha256=forged"}))
    raise SystemExit("FAIL: forged form webhook accepted")
except urllib.error.HTTPError as e:
    assert e.code == 401
assert json.loads(urllib.request.urlopen(urllib.request.Request(APP + "/webhooks/form", body, {"X-Signature": sig})).read())["outcome"] == "created"
print("PASS intake: Apollo + Sales Navigator exports imported, duplicates merged, invalid and suppressed rows skipped, signed form lead accepted, forged one refused")

wait(lambda: status("tim@unknown-startup.io") == "disqualified" and status("m.schulz@kaffeekontor.de") == "disqualified", "scoring")
assert "mailbox_not_found" in get(APP + "/leads?q=m.schulz")
assert status("info@greenleaf-cosmetics.de") == "disqualified" and status("s.collins@mega-retail-group.com") == "disqualified"
print("PASS scoring: enriched through a rate limit, dead mailbox and weak fits disqualified with reasons")

wait(lambda: len(inbox("lena@nordlicht-shop.de")) >= 1, "the first email in Lena's mailbox")
first = {a: inbox(a)[0] for a in ("lena@nordlicht-shop.de", "jonas.becker@kaffeekontor.de", "anna.gruber@bergwerk-outdoor.at",
                                    "clara.fischer@greenleaf-cosmetics.de", "luca@velo-atelier.ch", "mia@pfoten-glueck.de")}
lena = first["lena@nordlicht-shop.de"]
assert lena["Subject"] == "Nordlicht Shop + your order flow" and "runs on Shopify." in lena.get_content()
assert lena["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
assert not inbox("m.schulz@kaffeekontor.de") and not inbox("sven@competitor.de") and not inbox("info@greenleaf-cosmetics.de")
print("PASS sequence: personalised first email delivered to qualified leads only, with one-click unsubscribe headers")

answer(lena, "Sounds good. Could you do Tuesday at 10:00?")
answer(first["jonas.becker@kaffeekontor.de"], "Ich bin bis 30.12.2099 nicht im Büro.", subject="Automatische Antwort: Abwesend", Auto_Submitted="auto-replied")
answer(first["anna.gruber@bergwerk-outdoor.at"], "Bitte austragen, danke.")
answer(first["clara.fischer@greenleaf-cosmetics.de"], "Thanks, but we already have an agency for this.")
bounce = EmailMessage()
bounce["From"], bounce["To"], bounce["Subject"] = "MAILER-DAEMON@mx.tulpen-design.nl", "basar@outreach.example", "Undelivered Mail Returned to Sender"
bounce.set_content("Final-Recipient: rfc822; sanne@tulpen-design.nl\nAction: failed\nStatus: 5.1.1\n")
send(bounce)
link = re.search(r"(http://127\.0\.0\.1:8100/u/[\w.\-]+)", first["luca@velo-atelier.ch"].get_content()).group(1)
assert "You're unsubscribed" in urllib.request.urlopen(urllib.request.Request(link, b"List-Unsubscribe=One-Click", method="POST")).read().decode()
want = {"lena@nordlicht-shop.de": "interested", "jonas.becker@kaffeekontor.de": "in_sequence", "anna.gruber@bergwerk-outdoor.at": "unsubscribed",
        "clara.fischer@greenleaf-cosmetics.de": "not_interested", "sanne@tulpen-design.nl": "bounced", "luca@velo-atelier.ch": "unsubscribed"}
wait(lambda: all(status(a) == s for a, s in want.items()), "the replies to be sorted")
print("PASS replies: interested, out of office, unsubscribe, not interested and a bounce read over IMAP and sorted; unsubscribe link worked")

wait(lambda: any(c["properties"]["email"] == "lena@nordlicht-shop.de" for c in admin("/_admin/state")["crm"]["contacts"]), "the CRM handoff")
crm = admin("/_admin/state")["crm"]
assert len(crm["contacts"]) == 1 and "Tuesday at 10:00" in crm["deals"][0]["properties"]["description"]
print("PASS crm: only the interested lead became a contact + deal, with the reply on the deal")

wait(lambda: len(inbox("mia@pfoten-glueck.de")) == 3, "Mia's three emails")
mia = inbox("mia@pfoten-glueck.de")
assert mia[1]["Subject"] == "Re: " + mia[0]["Subject"] and mia[1]["In-Reply-To"] == mia[0]["Message-ID"] and mia[0]["Message-ID"] in mia[2]["References"]
wait(lambda: status("mia@pfoten-glueck.de") == "finished", "Mia's sequence to finish")
for a in ("lena@nordlicht-shop.de", "jonas.becker@kaffeekontor.de", "anna.gruber@bergwerk-outdoor.at", "clara.fischer@greenleaf-cosmetics.de", "luca@velo-atelier.ch"):
    assert len(inbox(a)) == 1, f"{a} got a follow-up after replying"
print("PASS follow-ups: steps 2 and 3 sent in the same thread to people who didn't answer; nobody who replied got another email")
PY
