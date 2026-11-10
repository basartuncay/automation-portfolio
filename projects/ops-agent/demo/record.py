"""Record a real walkthrough of the running stack (no mock-ups): an operator session, then a manager session.
Writes demo/out/operator.webm, demo/out/manager.webm, demo/out/markers.json and a few screenshots.

Run on a fresh stack:  docker compose down -v && ANTHROPIC_API_KEY= docker compose up -d --build --wait
                       docker compose exec -T web python -m ops_agent.evals
                       uv run --project ../js-scraper python demo/record.py
"""
import json, shutil, time
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
OUT, BASE = HERE / "out", "http://127.0.0.1:8093"
VIEW = {"width": 1440, "height": 900}
CURSOR = (HERE.parent.parent / "support-desk" / "demo" / "record.py").read_text().split('CURSOR = """')[1].split('"""')[0] \
    .replace("79,70,229", "109,40,217").replace("#4f46e5", "#6d28d9")


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

    def move_to(self, locator):
        locator.scroll_into_view_if_needed()
        box = locator.bounding_box()
        self.pg.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=25)
        self.pause(0.35)

    def click(self, locator, pause=0.6):
        self.move_to(locator)
        locator.click()
        self.pause(pause)

    def login(self, who):
        self.pg.goto(f"{BASE}/login")
        self.pause(0.5)
        self.pg.fill("input[type=email]", f"{who}@example.com")  # local demo seed account
        self.pg.fill("input[type=password]", "password")
        self.click(self.pg.locator("button.btn"))
        self.pg.wait_for_url(f"{BASE}/")
        self.pause(1)

    def task(self, text):
        box = self.pg.locator("textarea#task")
        self.click(box, pause=0.2)
        box.press_sequentially(text, delay=22)
        self.pause(0.4)
        self.click(self.pg.get_by_role("button", name="Run task"), pause=0.3)
        self.pg.wait_for_function("!document.querySelector('[data-status=queued],[data-status=running]')", timeout=30000)
        self.pause(0.8)

    def close(self):
        video = self.pg.video
        self.ctx.close()
        shutil.move(video.path(), OUT / f"{self.name}.webm")
        shutil.rmtree(OUT / f"raw-{self.name}", ignore_errors=True)
        return self.markers


def main() -> None:
    OUT.mkdir(exist_ok=True)
    markers = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()

        o = Session(browser, "operator")
        o.mark("login")
        o.login("operator")
        o.mark("task1")
        o.task("Refund €19.99 on order 1004 because the box arrived dented.")
        o.mark("trace1")
        o.pause(1.5)
        o.pg.mouse.wheel(0, 650)
        o.pause(3)
        o.pg.screenshot(path=str(OUT / "trace.png"))
        o.pg.goto(f"{BASE}/")
        o.mark("task2")
        o.task("Refund €120 on order 1012 because both items arrived broken.")
        o.mark("held")
        o.pg.mouse.wheel(0, 900)
        o.pause(1)
        o.move_to(o.pg.locator(".approval").first)
        o.pause(3)
        o.pg.screenshot(path=str(OUT / "held.png"))
        o.pg.goto(f"{BASE}/")
        o.mark("task3")
        o.task("Order 1013: the customer says the charger was missing from the box. Refund €12 for it.")
        o.mark("injection")
        o.pg.locator("pre.json", has_text="customer_note_untrusted").first.scroll_into_view_if_needed()
        o.pause(5)
        markers["operator"] = o.close()

        m = Session(browser, "manager")
        m.mark("manager_login")
        m.login("manager")
        m.mark("approvals")
        m.click(m.pg.get_by_role("link", name="Approvals"), pause=2.5)
        m.click(m.pg.get_by_role("button", name="Approve").first, pause=1.5)
        m.mark("resumed")
        m.click(m.pg.get_by_role("link", name="run").first, pause=1.5)
        m.pg.wait_for_function("document.querySelector('[data-status=done]')", timeout=30000)
        m.pause(1)
        m.pg.mouse.wheel(0, 1600)
        m.pause(3)
        m.mark("shop")
        m.click(m.pg.get_by_role("link", name="Shop activity"), pause=4)
        m.pg.screenshot(path=str(OUT / "shop.png"))
        m.mark("evals")
        m.click(m.pg.get_by_role("link", name="Evals"), pause=1)
        m.pg.screenshot(path=str(OUT / "evals.png"))
        m.pg.mouse.wheel(0, 400)
        m.pause(4)
        markers["manager"] = m.close()
        browser.close()

    (OUT / "markers.json").write_text(json.dumps(markers, indent=1))
    print(json.dumps(markers))


if __name__ == "__main__":
    main()
