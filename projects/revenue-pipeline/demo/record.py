"""Record a real walkthrough of the running stack (no mock-ups). Faults are injected into the stand-in sources through
their admin endpoint while the browser records the dashboard. Writes demo/out/pipeline.webm, markers and screenshots.

Run on a fresh stack:  docker compose down -v && docker compose up -d --build --wait   (wait for run 1 to succeed)
                       uv run --project ../js-scraper python demo/record.py
"""
import json, shutil, time, urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
OUT, WEB, SRC = HERE / "out", "http://127.0.0.1:8096", "http://127.0.0.1:8097"
ENV = dict(l.split("=", 1) for l in (HERE.parent / ".env").read_text().split())
VIEW = {"width": 1440, "height": 900}
CURSOR = (HERE.parent.parent / "support-desk" / "demo" / "record.py").read_text().split('CURSOR = """')[1].split('"""')[0] \
    .replace("79,70,229", "3,105,161").replace("#4f46e5", "#0369a1")


def chaos(**body):
    urllib.request.urlopen(urllib.request.Request(SRC + "/_admin/chaos", json.dumps(body).encode(),
                                                  {"x-admin": ENV["SOURCES_ADMIN_TOKEN"], "Content-Type": "application/json"}))


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

        def run_and_wait():
            before = pg.locator("[data-status]").count()
            click(pg.get_by_role("button", name="Run now"), 1)
            for _ in range(120):  # the page reloads itself while a run is going; also reload until the new run shows up
                if pg.locator("[data-status]").count() > before and pg.locator("[data-status=running]").count() == 0:
                    break
                pause(1)
                if pg.locator("[data-status=running]").count() == 0:
                    pg.reload()
            pause(1)

        pg.mouse.move(720, 450)
        mark("login")
        pg.goto(f"{WEB}/login")
        pg.fill("input[type=email]", "finance@example.com")  # local demo seed account
        pg.fill("input[type=password]", "password")
        click(pg.locator("button.btn"), 1.2)
        mark("dashboard")
        pause(4)
        pg.screenshot(path=str(OUT / "dashboard.png"))
        pg.mouse.wheel(0, 700)
        pause(4)
        pg.screenshot(path=str(OUT / "dashboard-bottom.png"))
        mark("model")
        click(pg.get_by_role("link", name="Data model"), 4)
        pg.screenshot(path=str(OUT / "model.png"))
        mark("runs")
        click(pg.get_by_role("link", name="Pipeline runs"), 1.5)
        chaos(negative_spend=True)
        mark("bad_data")
        run_and_wait()
        pause(3)
        pg.screenshot(path=str(OUT / "failed-run.png"))
        mark("banner")
        click(pg.get_by_role("link", name="Dashboard"), 4)
        pg.screenshot(path=str(OUT / "banner.png"))
        chaos(clear_negative_spend=True)
        chaos(renamed_column=True)
        mark("drift")
        click(pg.get_by_role("link", name="Pipeline runs"), 0.8)
        run_and_wait()
        pause(3.5)
        chaos(renamed_column=False)
        chaos(late_refund=5003)
        mark("recovered")
        run_and_wait()
        pause(2)
        click(pg.get_by_role("link", name="Dashboard"), 4)
        video = pg.video
        ctx.close()
        shutil.move(video.path(), OUT / "pipeline.webm")
        shutil.rmtree(OUT / "raw", ignore_errors=True)
        b.close()
    (OUT / "markers.json").write_text(json.dumps({"pipeline": marks}, indent=1))
    print(json.dumps(marks))


if __name__ == "__main__":
    main()
