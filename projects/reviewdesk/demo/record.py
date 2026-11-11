"""Record a real walkthrough of the running stack for the demo video (no mock-ups). Reviews come from the Google
Business Profile stand-in; replies are posted to it.

  docker compose down -v && POLL_SECONDS=1 SYNC_MINUTES=0.02 AUTO_DELAY_MINUTES=0.25 docker compose up -d --build --wait
  uv run --project ../js-scraper python demo/record.py
  cd demo/video && npx remotion render Demo ../../docs/demo.mp4 && npx remotion still Thumbnail ../../docs/demo-thumb.png
"""
import json, shutil, sys, time, urllib.request
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parents[2] / "tools"))
from demo_recorder import Recorder  # noqa: E402

ENV = dict(l.split("=", 1) for l in (HERE.parent / ".env").read_text().split())
APP = "http://127.0.0.1:8116"
META = {"file": "reviewdesk.webm", "who": "Real stack · local stand-ins for the Google Business Profile API and Slack", "accent": "#d98e04", "bg": "#14353a", "font": "'Iowan Old Style', Palatino, Georgia, serif",
        "kicker": "Reviewdesk · real recording", "title": "Every Google review answered, the hard ones by a person",
        "sub": "Drafts in the reviewer's language that fit what they wrote, praise answered on its own, criticism and serious claims held for the manager with an alert.",
        "outro": "Python · FastAPI · SQLite · Google Business Profile API · Slack webhooks · Docker · e2e in GitHub Actions.",
        "thumbKicker": "LOCAL BUSINESS", "thumbTitle": "Reviews answered, nothing risky auto-posted", "thumbSub": "Reviewdesk · Google reviews → drafts → approval"}

time.sleep(2)
with Recorder(HERE, META) as r:
    pg = r.page
    pg.goto(APP + "/login")
    pg.fill("input[name=email]", "manager@example.com"); pg.fill("input[name=password]", "password")
    r.click(pg.locator("button.btn"), 0.5)
    r.caption("New Google reviews for two locations. The serious ones wait for the manager, most urgent first")
    r.pause(4)
    r.caption("A food-poisoning claim: an alert went to Slack, and the draft takes it private without repeating the claim")
    r.move_to(pg.locator("[data-review='r006'] textarea"))
    r.pause(4)
    r.caption("A 2-star review in German: the draft answers each point, the salt, the wait and the price")
    r.move_to(pg.locator("[data-review='r004'] textarea"))
    r.pause(3.5)
    r.caption("The manager adds a personal line and posts it to Google")
    ta = pg.locator("[data-review='r004'] textarea")
    ta.click()
    ta.evaluate("e => { const i = e.value.lastIndexOf('\\n\\nMara'); e.setSelectionRange(i, i); }")  # before the signature
    ta.press_sequentially(" Ihr nächster Ramen geht auf uns.", delay=30)
    r.click(pg.locator("[data-review='r004'] button[value='post']"), 2)
    r.caption("Praise is answered on its own, after a window in which anyone can still edit it")
    r.click(pg.get_by_role("link", name="Scheduled"), 3)
    time.sleep(8)
    r.caption("Posted: in English or German, by first name, mentioning what they liked")
    r.click(pg.get_by_role("link", name="Posted"), 4)
    r.shot("posted.png")
    r.caption("Per location: rating, how many got a reply, how fast, and what the complaints are about")
    r.click(pg.get_by_role("link", name="Ratings by location"), 4)
    r.shot("stats.png")
    pg.goto(APP + "/?status=awaiting_approval")
    r.shot("approval.png")
for f in ("posted.png", "stats.png", "approval.png"):
    shutil.copy(HERE / "out" / f, HERE.parent / "docs" / f)
shutil.copy(HERE / "out" / "approval.png", HERE / "video" / "public" / "queue.png")
