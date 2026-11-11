"""Record a real walkthrough of the running stack for the demo video (no mock-ups). The customer's phone is the
Cloud API stand-in's chat view: typing and tapping there sends real signed webhooks to the app.

  docker compose down -v && docker compose up -d --build --wait
  uv run --project ../js-scraper python demo/record.py
  cd demo/video && npx remotion render Demo ../../docs/demo.mp4 && npx remotion still Thumbnail ../../docs/demo-thumb.png
"""
import json, shutil, sys, urllib.request
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parents[2] / "tools"))
from demo_recorder import Recorder  # noqa: E402

ENV = dict(l.split("=", 1) for l in (HERE.parent / ".env").read_text().split())
APP, APIS = "http://127.0.0.1:8108", "http://127.0.0.1:8109"
LENA, MAX = "4917612345678", "4917699887766"
phone = lambda n: f"{APIS}/phone/{n}?token={ENV['FAKES_ADMIN_TOKEN']}"
ship = lambda no: urllib.request.urlopen(urllib.request.Request(APIS + "/_admin/ship", json.dumps({"number": no}).encode(),
                                                                {"x-admin": ENV["FAKES_ADMIN_TOKEN"], "Content-Type": "application/json"}))
META = {"file": "chatline.webm", "who": "Real stack · local stand-ins for the WhatsApp Cloud API and the shop", "accent": "#7ef2c1", "bg": "#2b2870", "font": "'Avenir Next', Avenir, sans-serif",
        "kicker": "Chatline · real recording", "title": "WhatsApp customer service that knows when to hand over",
        "sub": "Order tracking and returns by tapping buttons, a person for everything else, and WhatsApp's 24-hour, template and opt-in rules built in.",
        "outro": "Python · FastAPI · SQLite · WhatsApp Cloud API · signed webhooks · Docker · e2e in GitHub Actions.",
        "thumbKicker": "WHATSAPP", "thumbTitle": "Orders and returns on WhatsApp", "thumbSub": "Chatline · bot + team inbox"}

with Recorder(HERE, META) as r:
    pg = r.page
    pg.goto(phone(LENA))
    r.caption("A customer asks on WhatsApp where her order is")
    r.type(pg.locator("#t"), "Hi, where is my order?", delay=45)
    r.click(pg.locator("#f button"), 2)
    r.caption("Two orders on her number: she picks one from a list, and gets the tracking link")
    r.click(pg.locator("button[data-id='track:1042']"), 2.5)
    r.caption("Then a return, by tapping: the order, the item, the reason")
    r.click(pg.locator("button[data-id='menu']").last, 2)
    r.click(pg.locator("button[data-id='return']").last, 2)
    r.click(pg.locator("button[data-id='ret:1031']").last, 2)
    r.click(pg.locator("button[data-id='item:SNK-42']").last, 2)
    r.click(pg.locator("button[data-id='size']").last, 2)
    r.caption("Registered with the shop, free label included. No person needed")
    r.pause(3)
    r.caption("Another customer has a complaint. The bot hands it to a person and stays quiet")
    pg.goto(phone(MAX))
    r.type(pg.locator("#t"), "The kettle I got is broken, I'm not happy", delay=40)
    r.click(pg.locator("#f button"), 3)
    pg.goto(APP + "/login")
    pg.fill("input[name=email]", "care@example.com"); pg.fill("input[name=password]", "password")
    r.click(pg.locator("button.btn"), 0.5)
    r.caption("The team inbox: what needs a person comes first")
    r.pause(2.5)
    r.click(pg.locator(f"a[data-conv='{MAX}']"), 1)
    r.shot("conversation-before.png")
    r.caption("Cleo answers from the inbox while the 24-hour window is open. Ticks show delivered and read")
    r.type(pg.locator("textarea[name=body]"), "Hi Max, Cleo here. Sorry about that! A replacement goes out today, no need to send it back.", delay=18)
    r.click(pg.get_by_role("button", name="Send"), 2.5)
    r.shot("conversation.png")
    r.caption("He opts in to updates with START. When the order ships, an approved template goes out")
    pg.goto(phone(MAX))
    r.type(pg.locator("#t"), "Thanks! START", delay=45)
    r.click(pg.locator("#f button"), 2)
    ship("1050")
    r.pause(4.5)
    r.shot("phone.png")
    pg.goto(APP + "/?view=all")
    r.caption("Every conversation, who solved it, and how")
    r.pause(4)
    r.shot("inbox.png")
for f in ("conversation.png", "phone.png", "inbox.png"):
    shutil.copy(HERE / "out" / f, HERE.parent / "docs" / f)
shutil.copy(HERE / "out" / "conversation.png", HERE / "video" / "public" / "queue.png")
