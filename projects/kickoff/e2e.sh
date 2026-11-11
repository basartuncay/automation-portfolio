#!/usr/bin/env bash
# End-to-end in Docker with a real mail server: an enquiry on the public form, the studio edits and sends the
# proposal, the client opens it from the email, signs (a stale document fingerprint is refused), gets the signed PDF,
# pays the deposit on the Stripe stand-in, and the project, shared folder and welcome email are set up through an
# outage. Forged and replayed Stripe webhooks are refused; an unopened proposal gets its reminder.
set -euo pipefail
cd "$(dirname "$0")"
./setup.sh >/dev/null
docker compose down -v >/dev/null 2>&1 || true
POLL_SECONDS=1 BACKOFF_SCALE=0.2 DAY_SECONDS_SCALE=0.00005 docker compose up -d --build --wait --wait-timeout 300
trap '[ "${KEEP:-1}" = 0 ] && docker compose down -v >/dev/null 2>&1 || true' EXIT
set -a; . ./.env; set +a

python3 - <<'PY'
import email, email.policy, imaplib, json, os, re, time, urllib.error, urllib.parse, urllib.request
from http.cookiejar import CookieJar

APP, APIS = "http://127.0.0.1:8104", "http://127.0.0.1:8105"
H = {"x-admin": os.environ["FAKES_ADMIN_TOKEN"], "Content-Type": "application/json"}
admin = lambda path, body=None: json.loads(urllib.request.urlopen(urllib.request.Request(APIS + path, json.dumps(body).encode() if body is not None else None, H)).read())

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None
def browser(follow=True):
    handlers = [urllib.request.HTTPCookieProcessor(CookieJar())] + ([] if follow else [NoRedirect()])
    return urllib.request.build_opener(*handlers)
def get(b, url):
    return b.open(url).read().decode()
def post(b, url, page, data, follow=True):
    token = re.search(r'name="csrf" value="([^"]+)"', get(b, page)).group(1)
    body = urllib.parse.urlencode({**data, "csrf": token}, doseq=True).encode()
    try:
        r = b.open(url, body)
        return r.geturl(), r.read().decode()
    except urllib.error.HTTPError as e:
        if e.code in (302, 303):
            return e.headers["Location"], ""
        raise
def inbox(addr):
    m = imaplib.IMAP4("127.0.0.1", 3147)
    try:
        m.login(addr, "any"); m.select("INBOX")
        return [email.message_from_bytes(m.fetch(i, "(RFC822)")[1][0][1], policy=email.policy.default) for i in m.search(None, "ALL")[1][0].split()]
    finally:
        m.logout()
def wait(cond, what, tries=90):
    for _ in range(tries):
        try:
            if cond():
                return
        except (OSError, imaplib.IMAP4.error, AttributeError, KeyError, IndexError):
            pass
        time.sleep(1)
    raise SystemExit(f"FAIL: timed out waiting for {what}")
body_of = lambda m: m.get_body(preferencelist=("plain",)).get_content()

guest = browser()
brief = "Our Shopify orders should land in weclapp automatically and stock should flow back. Today two people copy them every morning."
_, page = post(guest, APP + "/enquire", APP + "/", {"name": "Lena Hoffmann", "email": "lena@nordlicht-shop.de", "company": "Nordlicht Shop",
                                                    "brief": brief, "package": "1", "options": ["monitoring"]})
assert "on my desk" in page
_, page = post(guest, APP + "/enquire", APP + "/", {"name": "Tom Weber", "email": "tom@slow-reply.de", "company": "Slow Reply GmbH",
                                                    "brief": "We need our invoices from three tools in one accounting export, monthly.", "package": "2"})
wait(lambda: any("New enquiry: Nordlicht Shop" in m["Subject"] for m in inbox("basar@studio.example")), "the studio's enquiry email")
print("PASS enquiry: public form created a client with a draft proposal from the package; the studio got an email")

studio = browser(follow=False)
post(studio, APP + "/login", APP + "/login", {"email": "studio@example.com", "password": "password"})
board = get(studio, APP + "/board")
lena_id = re.search(r'href="/clients/(\d+)" data-stage="enquiry"><b>Nordlicht Shop', board).group(1)
tom_id = re.search(r'href="/clients/(\d+)" data-stage="enquiry"><b>Slow Reply', board).group(1)
post(studio, f"{APP}/clients/{lena_id}/proposal", f"{APP}/clients/{lena_id}", {"intro": "Hi Lena, as discussed on the call.", "deposit_pct": "30",
     "item_name": ["Store integration (Shopify + weclapp)", "Monitoring & alerts for 3 months", "Staff training (2 h)", ""],
     "item_detail": ["Orders, stock and invoices, two-way", "", "", ""], "item_amount": ["3200", "450", "240", ""]})
post(studio, f"{APP}/clients/{lena_id}/send", f"{APP}/clients/{lena_id}", {})
post(studio, f"{APP}/clients/{tom_id}/send", f"{APP}/clients/{tom_id}", {})
wait(lambda: inbox("lena@nordlicht-shop.de"), "the proposal email")
mail = inbox("lena@nordlicht-shop.de")[0]
assert mail["Subject"] == "Your proposal: €3,890.00", mail["Subject"]
link = re.search(r"(http://127\.0\.0\.1:8104/p/[\w\-]+)", body_of(mail)).group(1)
print("PASS proposal: studio edited the draft (3 lines, €3,890) and sent it; the client got the link by email")

client = browser(follow=False)
page = get(client, link)
assert "Staff training (2 h)" in page and "€3,890.00" in page
assert 'data-stage="viewed"' in get(studio, f"{APP}/clients/{lena_id}")
sha = re.search(r'name="sha256" value="(\w+)"', page).group(1)
loc, _ = post(client, link + "/sign", link, {"typed_name": "Lena Hoffmann", "agree": "1", "sha256": "0" * 64})
assert "changed+while+you+had+it+open" in loc or "changed%20while" in loc, loc
loc, _ = post(client, link + "/sign", link, {"typed_name": "Lena Hoffmann", "agree": "1", "sha256": sha})
assert loc.startswith("http://127.0.0.1:8105/stripe/pay/cs_test_"), loc
wait(lambda: any(m["Subject"].startswith("Signed") for m in inbox("lena@nordlicht-shop.de")), "the signed copy")
signed = next(m for m in inbox("lena@nordlicht-shop.de") if m["Subject"].startswith("Signed"))
pdf = next(signed.iter_attachments()).get_content()
assert pdf.startswith(b"%PDF") and sha[:16] in body_of(signed)
open("docs/signed-example.pdf", "wb").write(pdf)
print(f"PASS signing: a stale fingerprint was refused; signed with sha256 {sha[:12]}…, signed PDF emailed, sent on to Stripe Checkout")

admin("/_admin/chaos", {"projects_fail": 2})
pay_page = urllib.request.urlopen(loc).read().decode()
assert "€1,167.00" in pay_page  # 30% of €3,890
urllib.request.urlopen(urllib.request.Request(loc, b"", method="POST")).read()
wait(lambda: 'data-stage="onboarded"' in get(studio, f"{APP}/clients/{lena_id}"), "provisioning to finish")
st = admin("/_admin/state")
assert len(st["projects"]) == 1 and len(st["tasks"]["prj_1"]) == 5 and st["permissions"]["fld_1"][0]["emailAddress"] == "lena@nordlicht-shop.de"
wait(lambda: any(m["Subject"] == "Welcome aboard: next steps" for m in inbox("lena@nordlicht-shop.de")), "the welcome email")
welcome = body_of(next(m for m in inbox("lena@nordlicht-shop.de") if m["Subject"] == "Welcome aboard: next steps"))
assert "projects.example.com/p/1" in welcome and "drive.example.com/folders/1" in welcome
assert "step retry" in get(studio, f"{APP}/clients/{lena_id}")
print("PASS payment + setup: deposit paid, project (5 tasks) and shared folder created through 2 outages, welcome email with both links")

assert admin("/_admin/stripe/replay", {})["status"] == 200  # Stripe retries the event: answered, booked once
assert admin("/_admin/stripe/replay", {"old": True})["status"] == 400
assert admin("/_admin/stripe/replay", {"forged": True})["status"] == 400
assert len(admin("/_admin/state")["projects"]) == 1
print("PASS webhooks: a retried event changed nothing; an hour-old replay and a forged signature were refused")

wait(lambda: any(m["Subject"] == "Did the proposal reach you?" for m in inbox("tom@slow-reply.de")), "the not-opened reminder")
assert not any(m["Subject"] == "Did the proposal reach you?" for m in inbox("lena@nordlicht-shop.de"))
print("PASS reminders: the unopened proposal got a reminder; the client who signed never got one")
PY
