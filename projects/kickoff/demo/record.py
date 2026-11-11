"""Record a real walkthrough of the running stack for the demo video (no mock-ups): the client fills the public form,
the studio sends the proposal, the client signs and pays on the Stripe stand-in, and the setup runs.

  docker compose down -v && POLL_SECONDS=1 BACKOFF_SCALE=0.2 docker compose up -d --build --wait
  uv run --project ../js-scraper python demo/record.py
  cd demo/video && npx remotion render Demo ../../docs/demo.mp4 && npx remotion still Thumbnail ../../docs/demo-thumb.png
"""
import email, email.policy, imaplib, json, re, sys, time, urllib.request
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parents[2] / "tools"))
from demo_recorder import Recorder  # noqa: E402

ENV = dict(l.split("=", 1) for l in (HERE.parent / ".env").read_text().split())
APP, APIS = "http://127.0.0.1:8104", "http://127.0.0.1:8105"


def link_from_mail(addr):
    for _ in range(60):
        m = imaplib.IMAP4("127.0.0.1", 3147)
        try:
            m.login(addr, "x"); m.select("INBOX")
            for i in m.search(None, "ALL")[1][0].split():
                msg = email.message_from_bytes(m.fetch(i, "(RFC822)")[1][0][1], policy=email.policy.default)
                found = re.search(r"(http://127\.0\.0\.1:8104/p/[\w\-]+)", msg.get_body(preferencelist=("plain",)).get_content())
                if found:
                    return found.group(1)
        finally:
            m.logout()
        time.sleep(0.5)
    raise SystemExit("no proposal email")


urllib.request.urlopen(urllib.request.Request(APIS + "/_admin/chaos", json.dumps({"projects_fail": 2}).encode(),
                                              {"x-admin": ENV["FAKES_ADMIN_TOKEN"], "Content-Type": "application/json"}))
META = {"file": "kickoff.webm", "who": "Real stack · real mail server · local stand-ins for Stripe, the project tool and storage", "accent": "#2f7d65",
        "kicker": "Kickoff · real recording", "title": "From \"can you help us?\" to a signed, paid, set-up project",
        "sub": "Proposal from an enquiry, e-signature bound to the exact text, signed PDF, Stripe deposit, and the project, folder and welcome email set up on their own.",
        "outro": "Python · FastAPI · SQLite · Stripe Checkout + signed webhooks · fpdf2 · SMTP · Docker · e2e in GitHub Actions.",
        "thumbKicker": "CLIENT ONBOARDING", "thumbTitle": "Signed, paid and set up, hands off", "thumbSub": "Kickoff · proposal → e-sign → Stripe → setup"}

with Recorder(HERE, META) as r:
    pg = r.page
    pg.goto(APP + "/")
    r.caption("A prospect asks for a proposal on the studio's site")
    r.pause(1)
    r.click(pg.locator("input[value='rush']"), 0.3)
    r.type(pg.locator("#brief"), "Our Shopify orders should land in weclapp automatically, and stock should flow back. Two people copy them every morning.", delay=8)
    r.type(pg.locator("#name"), "Lena Hoffmann")
    r.type(pg.locator("#email"), "lena@nordlicht-shop.de")
    r.type(pg.locator("#company"), "Nordlicht Shop")
    r.click(pg.get_by_role("button", name="Ask for a proposal"), 1.5)
    pg.goto(APP + "/login")
    pg.fill("input[name=email]", "studio@example.com"); pg.fill("input[name=password]", "password")
    r.click(pg.locator("button.btn"), 0.3)
    r.caption("The studio gets a draft built from the package and the options she picked")
    r.pause(2)
    r.click(pg.locator("a.cardlet", has_text="Nordlicht Shop"), 1)
    r.caption("Adjust a line, then send. She gets a link by email")
    pg.locator("input[name=item_name]").last.fill("")
    r.type(pg.locator("input[name=item_name]").last, "Staff training (2 h)")
    r.type(pg.locator("input[name=item_amount]").last, "240")
    r.click(pg.get_by_role("button", name="Save draft"), 1)
    r.click(pg.get_by_role("button", name=re.compile("Send proposal")), 1.5)
    link = link_from_mail("lena@nordlicht-shop.de")
    r.caption("Lena opens the link. Signing ties her name to the SHA-256 of exactly this text")
    pg.goto(link)
    r.pause(2)
    pg.mouse.wheel(0, 900)
    r.pause(1)
    r.type(pg.locator("#typed"), "Lena Hoffmann", delay=60)
    r.click(pg.locator("input[name=agree]"), 0.5)
    r.click(pg.get_by_role("button", name=re.compile("Sign")), 1.5)
    r.caption("Signed. The signed PDF is emailed, and she lands on Stripe Checkout for the 30% deposit")
    r.pause(3)
    r.click(pg.get_by_role("button", name=re.compile("Pay")), 0.4)
    r.caption("Paid. Stripe's signed webhook, not the redirect, marks it paid")
    r.pause(4)
    r.caption("Then the setup runs by itself: project with tasks, shared folder, welcome email, through 2 outages")
    time.sleep(3)
    pg.goto(APP + "/board")
    r.click(pg.locator("a.cardlet", has_text="Nordlicht Shop"), 3)
    r.caption("Signature record, deposit, setup steps, and every email: sent, or cancelled because it no longer fit")
    pg.mouse.wheel(0, 500)
    r.pause(5)
