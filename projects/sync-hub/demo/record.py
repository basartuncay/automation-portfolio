"""Record a real walkthrough of the running stack (no mock-ups). The store and CRM are the local stand-ins; changes
"made in the store / in the CRM" are made through their admin endpoints while the browser records the hub.
Writes demo/out/hub.webm, demo/out/markers.json and screenshots.

Run on a fresh stack:  docker compose down -v && MAX_ATTEMPTS=3 BACKOFF_SECONDS=1 POLL_SECONDS=1 docker compose up -d --build --wait
                       uv run --project ../js-scraper python demo/record.py
"""
import json, shutil, time, urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
OUT, HUB, APIS = HERE / "out", "http://127.0.0.1:8094", "http://127.0.0.1:8095"
ENV = dict(l.split("=", 1) for l in (HERE.parent / ".env").read_text().split())
VIEW = {"width": 1440, "height": 900}
CURSOR = (HERE.parent.parent / "support-desk" / "demo" / "record.py").read_text().split('CURSOR = """')[1].split('"""')[0] \
    .replace("79,70,229", "194,65,12").replace("#4f46e5", "#c2410c")


def admin(path, body=None):
    req = urllib.request.Request(APIS + path, json.dumps(body).encode() if body is not None else None,
                                 {"x-admin": ENV["FAKES_ADMIN_TOKEN"], "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())


def until(cond, tries=60):
    for _ in range(tries):
        if cond():
            return
        time.sleep(0.5)
    raise SystemExit("timed out")


def contact(email):
    return next((c for c in admin("/crm/_admin/state")["contacts"] if c["properties"].get("email") == email), None)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    marks, t0 = [], time.monotonic()
    mark = lambda s: marks.append({"scene": s, "t": round(time.monotonic() - t0, 2)})
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport=VIEW, record_video_dir=str(OUT / "raw"), record_video_size=VIEW)
        ctx.add_init_script(CURSOR)
        pg = ctx.new_page()
        pause = lambda s: pg.wait_for_timeout(int(s * 1000))

        def click(loc, after=0.8):
            loc.scroll_into_view_if_needed()
            box = loc.bounding_box()
            pg.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=25)
            pause(0.35)
            loc.click()
            pause(after)

        pg.mouse.move(720, 450)
        mark("login")
        pg.goto(f"{HUB}/login")
        pg.fill("input[type=email]", "ops@example.com")  # local demo seed account
        pg.fill("input[type=password]", "password")
        click(pg.locator("button.btn"), 1.5)
        mark("connect")
        click(pg.get_by_role("button", name="Connect CRM"), 2.5)
        mark("consent")
        click(pg.get_by_role("button", name="Allow access"), 2.5)
        mark("connected")
        ana = admin("/shop/_admin/customers", {"email": "ana.silva@example.com", "first_name": "Ana", "last_name": "Silva", "phone": "+351 912 345 678"})
        admin("/shop/_admin/customers", {"email": "ben.okoro@example.com", "first_name": "Ben", "last_name": "Okoro", "phone": "0044 20 7946 0018"})
        until(lambda: contact("ben.okoro@example.com"))
        pg.goto(f"{HUB}/")
        pause(4.5)
        mark("both_sides")
        pg.goto(f"{APIS}/_view?token={ENV['FAKES_ADMIN_TOKEN']}")
        pause(3)
        cid = contact("ana.silva@example.com")["id"]
        admin(f"/crm/_admin/contacts/{cid}", {"lastname": "Silva Costa", "vip": "true"})
        admin("/shop/_admin/orders", {"customer_id": ana["id"], "amount": 89.9})
        mark("edits_flow")
        pause(5.5)
        pg.screenshot(path=str(OUT / "both-systems.png"))
        mark("log")
        pg.goto(f"{HUB}/log")
        pause(5)
        pg.screenshot(path=str(OUT / "log.png"))
        admin(f"/shop/_admin/customers/{ana['id']}", {"phone": "+351 900 000 001", "silent": True})
        admin(f"/crm/_admin/contacts/{cid}", {"phone": "+351 900 000 002"})
        time.sleep(3)  # a poll cycle: the hub sees the CRM edit, reads the store before writing, finds the conflict
        mark("conflict")
        click(pg.get_by_role("link", name="Conflicts"), 2.5)
        pg.screenshot(path=str(OUT / "conflict.png"))
        click(pg.get_by_role("button", name="Keep store value").first, 2.5)
        admin("/crm/_admin/fail", {"mode": "503", "count": 999})
        admin("/shop/_admin/customers", {"email": "zoe.martin@example.com", "first_name": "Zoe", "last_name": "Martin"})
        time.sleep(5)
        mark("dead")
        click(pg.get_by_role("link", name="Dead letters"), 3)
        pg.screenshot(path=str(OUT / "dead.png"))
        admin("/crm/_admin/fail", {"mode": "ok"})
        mark("replay")
        click(pg.get_by_role("button", name="Replay all"), 1)
        until(lambda: contact("zoe.martin@example.com"))
        click(pg.get_by_role("link", name="Overview"), 4)
        pg.screenshot(path=str(OUT / "overview.png"))
        mark("final")
        pg.goto(f"{APIS}/_view?token={ENV['FAKES_ADMIN_TOKEN']}")
        pause(5)
        video = pg.video
        ctx.close()
        shutil.move(video.path(), OUT / "hub.webm")
        shutil.rmtree(OUT / "raw", ignore_errors=True)
        b.close()
    (OUT / "markers.json").write_text(json.dumps({"hub": marks}, indent=1))
    print(json.dumps(marks))


if __name__ == "__main__":
    main()
