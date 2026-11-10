"""Record a real walkthrough of the running stack (no mock-ups). Before recording, a few real rounds of competitor
price changes and crawls build up some price history; then one session is recorded.
Writes demo/out/watch.webm and demo/out/markers.json (scene start times in seconds).

Run on a fresh stack:  docker compose up -d --build --wait
                       uv run --project ../js-scraper python demo/record.py
"""
import email, imaplib, json, os, shutil, subprocess, time, urllib.request
from email import policy
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
OUT, BASE, SHOP = HERE / "out", "http://127.0.0.1:8091", "http://127.0.0.1:8092"
TOKEN = dict(l.split("=", 1) for l in (HERE.parent / ".env").read_text().split())["SHOP_ADMIN_TOKEN"]
VIEW = {"width": 1440, "height": 900}
CURSOR = (HERE.parent.parent / "support-desk" / "demo" / "record.py").read_text().split('CURSOR = """')[1].split('"""')[0] \
    .replace("79,70,229", "13,148,136").replace("#4f46e5", "#0d9488")


def db(sql: str) -> list:
    py = f"import json,sqlite3; c=sqlite3.connect('/data/watch.db'); c.row_factory=sqlite3.Row; print(json.dumps([dict(r) for r in c.execute({sql!r})]))"
    return json.loads(subprocess.run(["docker", "compose", "exec", "-T", "web", "python", "-c", py], cwd=HERE.parent,
                                     capture_output=True, text=True, check=True).stdout)


def shop(path, body):
    req = urllib.request.Request(SHOP + path, json.dumps(body).encode(), {"Content-Type": "application/json", "x-token": TOKEN})
    urllib.request.urlopen(req).read()


def crawl_and_wait():
    n = len(db("select id from runs where finished is not null"))
    py = "import sqlite3; c=sqlite3.connect('/data/watch.db'); c.execute(\"insert into requests (at, kind) values (datetime('now'), 'crawl')\"); c.commit()"
    subprocess.run(["docker", "compose", "exec", "-T", "web", "python", "-c", py], cwd=HERE.parent, check=True)
    while len(db("select id from runs where finished is not null")) < n + 2:
        time.sleep(0.5)
    time.sleep(1.1)  # distinct timestamps per round on the chart


def history_rounds():
    """Real crawls of real (demo) price moves, so the chart has history. Nothing here is undercut yet."""
    for vm, gh in [(131.90, 134.04), (129.90, 132.50), (129.90, 131.00)]:
        shop("/voltmart/admin/set", {"pid": "wireless-headphones-x200", "price": vm})
        shop("/gadgethub/admin/set", {"pid": "wireless-headphones-x200", "price": gh})
        crawl_and_wait()


class Session:
    def __init__(self, browser, name):
        self.name, self.markers, self.t0 = name, [], time.monotonic()
        self.ctx = browser.new_context(viewport=VIEW, record_video_dir=str(OUT / f"raw-{name}"), record_video_size=VIEW)
        self.ctx.add_init_script(CURSOR)
        self.pg = self.ctx.new_page()
        self.pg.mouse.move(720, 450)

    def mark(self, scene):
        self.markers.append({"scene": scene, "t": round(time.monotonic() - self.t0, 2)})

    def pause(self, s):
        self.pg.wait_for_timeout(int(s * 1000))

    def click(self, locator, pause=0.6):
        locator.scroll_into_view_if_needed()
        box = locator.bounding_box()
        self.pg.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=25)
        self.pause(0.35)
        locator.click()
        self.pause(pause)

    def close(self):
        video = self.pg.video
        self.ctx.close()
        shutil.move(video.path(), OUT / f"{self.name}.webm")
        shutil.rmtree(OUT / f"raw-{self.name}", ignore_errors=True)
        return self.markers


def digest_html() -> str:
    imap = imaplib.IMAP4("127.0.0.1", 3144)
    imap.login("pricing-team@shop.example", "pricing-team@shop.example")
    imap.select("INBOX")
    last = imap.search(None, "ALL")[1][0].split()[-1]
    msg = email.message_from_bytes(imap.fetch(last, "(RFC822)")[1][0][1], policy=policy.default)
    return f"<div style='zoom:1.5'><div style='font:14px system-ui;color:#555;padding:24px 32px 0'>From: {msg['From']}<br>To: {msg['To']}<br>" \
           f"Subject: <b>{msg['Subject']}</b></div><div style='padding:0 32px'>{msg.get_body(('html',)).get_content()}</div></div>"


def main() -> None:
    assert len(db("select id from runs")) == 2, "start from a fresh stack (only the baseline crawl done)"
    history_rounds()
    OUT.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        s = Session(browser, "watch")
        s.mark("login")
        s.pg.goto(f"{BASE}/login")
        s.pause(0.6)
        s.pg.fill("input[type=email]", "pricing@example.com")  # local demo seed account
        s.pg.fill("input[type=password]", "password")
        s.click(s.pg.locator("button.btn"))
        s.pg.wait_for_url(f"{BASE}/")
        s.mark("cheapest")
        s.pause(4)
        shop("/voltmart/admin/set", {"pid": "wireless-headphones-x200", "price": 119.00})
        shop("/gadgethub/admin/set", {"pid": "usb-c-charger-65w", "in_stock": False})
        shop("/gadgethub/admin/set", {"pid": "27-inch-4k-monitor", "price": 299.00})
        s.mark("crawl")
        s.click(s.pg.get_by_role("button", name="Crawl now"), pause=1)
        while not db("select id from alerts where kind = 'undercut'") or len(db("select id from alerts where kind = 'stock'")) < 1:
            time.sleep(0.5)
        time.sleep(0.5)
        s.pg.goto(f"{BASE}/")
        s.mark("undercut")
        s.pause(1)
        s.pg.screenshot(path=str(OUT / "dashboard.png"))
        s.pause(4)
        s.mark("history")
        s.click(s.pg.locator("tbody tr a", has_text="Wireless Headphones X200"), pause=1)
        s.pg.screenshot(path=str(OUT / "product.png"))
        s.pause(4)
        s.mark("alerts")
        s.click(s.pg.get_by_role("link", name="Alerts"), pause=4)
        shop("/admin/layout", {"broken": True})
        s.mark("redesign")
        s.click(s.pg.get_by_role("link", name="Products"), pause=0.6)
        s.click(s.pg.get_by_role("button", name="Crawl now"), pause=1)
        while len(db("select id from runs where status = 'suspicious'")) < 2:
            time.sleep(0.5)
        s.click(s.pg.get_by_role("link", name="Crawl health"), pause=4.5)
        s.pg.screenshot(path=str(OUT / "crawl-health.png"))
        shop("/admin/layout", {"broken": False})
        crawl_and_wait()  # the site is back: one clean crawl before the digest
        s.mark("digest")
        s.click(s.pg.get_by_role("link", name="Products"), pause=0.6)
        s.click(s.pg.get_by_role("button", name="Email weekly digest"), pause=1.5)
        for _ in range(40):
            try:
                html = digest_html()
                break
            except (imaplib.IMAP4.error, IndexError):
                time.sleep(0.5)
        s.mark("email")
        s.pg.set_content(html)
        s.pause(5)
        markers = {"watch": s.close()}
        browser.close()
    (OUT / "markers.json").write_text(json.dumps(markers, indent=1))
    print(json.dumps(markers))


if __name__ == "__main__":
    main()
