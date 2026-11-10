"""Record a real walkthrough of the running stack (no mock-ups): an agent session, then a lead session.
Writes demo/out/agent.webm, demo/out/lead.webm and demo/out/markers.json (scene start times in seconds).

Run on a fresh stack:  ANTHROPIC_API_KEY= docker compose up -d --build --wait
                       uv run --project ../js-scraper python demo/record.py
"""
import json, shutil, smtplib, subprocess, time
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
OUT, BASE = HERE / "out", "http://127.0.0.1:8090"
VIEW = {"width": 1440, "height": 900}
CURSOR = """
window.addEventListener('DOMContentLoaded', () => {
  const c = document.createElement('div');
  c.style.cssText = 'position:fixed;z-index:99999;width:22px;height:22px;border-radius:50%;pointer-events:none;' +
    'background:rgba(79,70,229,.3);border:2px solid #4f46e5;transform:translate(-50%,-50%);left:-50px;top:-50px;' +
    'transition:width .12s,height .12s';
  document.body.appendChild(c);
  document.addEventListener('mousemove', e => { c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px'; });
  document.addEventListener('mousedown', () => { c.style.width = c.style.height = '14px'; });
  document.addEventListener('mouseup', () => { c.style.width = c.style.height = '22px'; });
});
"""


def db(sql: str) -> list:
    py = f"import json,sqlite3; c=sqlite3.connect('/data/support.db'); c.row_factory=sqlite3.Row; print(json.dumps([dict(r) for r in c.execute({sql!r})]))"
    out = subprocess.run(["docker", "compose", "exec", "-T", "web", "python", "-c", py], cwd=HERE.parent,
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


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

    def hover(self, locator, pause=1.0):
        box = locator.bounding_box()
        self.pg.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=25)
        self.pause(pause)

    def login(self, email):
        self.pg.goto(f"{BASE}/login")
        self.pause(0.8)
        self.pg.fill("input[type=email]", email)  # local demo seed account
        self.pg.fill("input[type=password]", "password")
        self.click(self.pg.locator("button.btn"))
        self.pg.wait_for_url(f"{BASE}/")
        self.pause(1.2)

    def open_ticket(self, subject, pause=1.2):
        self.click(self.pg.locator("tbody tr a", has_text=subject).first, pause=pause)

    def close(self):
        video = self.pg.video
        self.ctx.close()
        shutil.move(video.path(), OUT / f"{self.name}.webm")
        shutil.rmtree(OUT / f"raw-{self.name}", ignore_errors=True)
        return self.markers


def main() -> None:
    assert not db("select id from tickets"), "start from a fresh stack: docker compose down -v && up"
    OUT.mkdir(exist_ok=True)
    markers = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()

        a = Session(browser, "agent")
        a.mark("login")
        a.login("agent@example.com")
        a.mark("empty")
        a.pause(2.5)
        with smtplib.SMTP("127.0.0.1", 3025) as s:
            for f in sorted((HERE.parent / "samples").glob("*.eml")):
                s.sendmail("customer@example.com", ["support@shop.example"], f.read_bytes())
        a.mark("emails_sent")
        for _ in range(60):
            if len(db("select id from tickets")) >= 6:
                break
            time.sleep(0.5)
        a.pg.goto(f"{BASE}/?status=all")
        a.mark("arrived")
        a.pause(5)
        a.mark("draft")
        a.open_ticket("Refund for my returned headphones", pause=2)
        a.hover(a.pg.locator("ol.sources li").first, pause=3)
        a.hover(a.pg.locator("textarea"), pause=1.5)
        a.mark("send")
        a.click(a.pg.get_by_role("button", name="Approve & send"), pause=3)
        a.mark("injection")
        a.pg.goto(f"{BASE}/?status=all")
        a.pause(0.8)
        a.open_ticket("Order 5521", pause=4)
        a.mark("lead_rule")
        a.pg.goto(f"{BASE}/?status=all")
        a.pause(0.8)
        a.open_ticket("Express shipping", pause=1)
        a.hover(a.pg.get_by_role("button", name="Approve & send"), pause=3)
        a.mark("gaps")
        a.click(a.pg.get_by_role("link", name="Knowledge base"), pause=4)
        markers["agent"] = a.close()

        b = Session(browser, "lead")
        b.mark("lead_login")
        b.login("lead@example.com")
        b.mark("lead_send")
        b.open_ticket("Express shipping", pause=1.5)
        b.click(b.pg.get_by_role("button", name="Approve & send"), pause=2.5)
        b.mark("done")
        b.click(b.pg.get_by_role("link", name="All tickets"), pause=4)
        markers["lead"] = b.close()
        browser.close()

    (OUT / "markers.json").write_text(json.dumps(markers, indent=1))
    sent = {r["subject"] for r in db("select subject from tickets where status = 'sent'")}
    assert sent == {"Refund for my returned headphones", "Express shipping - need it today"}, sent
    print(json.dumps(markers, indent=1))


if __name__ == "__main__":
    main()
