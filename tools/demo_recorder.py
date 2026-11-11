"""Shared helper for the demo recordings: a real browser session against the running stack, recorded with
Playwright, with a visible cursor and timed captions. Writes the recording into demo/video/public and the
caption track into demo/video/src/demo.json, which the generic Remotion composition renders.

Nothing here fakes a screen: every frame is the real app responding to real requests."""
import json, shutil, time
from pathlib import Path

from playwright.sync_api import sync_playwright

CURSOR = """
window.addEventListener('DOMContentLoaded', () => {
  const c = document.createElement('div');
  c.style.cssText = 'position:fixed;z-index:99999;width:22px;height:22px;border-radius:50%;pointer-events:none;' +
    'background:ACCENT33;border:2px solid ACCENT;transform:translate(-50%,-50%);left:-50px;top:-50px;transition:width .12s,height .12s';
  document.body.appendChild(c);
  document.addEventListener('mousemove', e => { c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px'; });
  document.addEventListener('mousedown', () => { c.style.width = c.style.height = '14px'; });
  document.addEventListener('mouseup', () => { c.style.width = c.style.height = '22px'; });
});
"""


class Recorder:
    def __init__(self, demo_dir: Path, meta: dict, view=(1440, 900)):
        self.demo, self.meta, self.view = demo_dir, meta, {"width": view[0], "height": view[1]}
        self.caps, self.t0 = [], None
        self.out = demo_dir / "out"

    def __enter__(self):
        self.out.mkdir(exist_ok=True)
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch()
        self.ctx = self.browser.new_context(viewport=self.view, record_video_dir=str(self.out / "raw"), record_video_size=self.view)
        self.ctx.add_init_script(CURSOR.replace("ACCENT", self.meta.get("accent", "#4f46e5")))
        self.page = self.ctx.new_page()
        self.t0 = time.monotonic()
        self.page.mouse.move(720, 450)
        return self

    def caption(self, text: str) -> None:
        self.caps.append({"from": round(time.monotonic() - self.t0, 2), "text": text})

    def pause(self, s: float) -> None:
        self.page.wait_for_timeout(int(s * 1000))

    def move_to(self, loc) -> None:
        loc.scroll_into_view_if_needed()
        box = loc.bounding_box()
        self.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=25)
        self.pause(0.3)

    def click(self, loc, after: float = 0.8) -> None:
        self.move_to(loc)
        loc.click()
        self.pause(after)

    def type(self, loc, text: str, delay: int = 35) -> None:
        self.move_to(loc)
        loc.click()
        loc.press_sequentially(text, delay=delay)

    def shot(self, name: str) -> None:
        self.page.screenshot(path=str(self.out / name))

    def __exit__(self, *exc):
        end = round(time.monotonic() - self.t0, 2)
        video = self.page.video
        self.ctx.close()
        name = self.meta["file"]
        public = self.demo / "video" / "public"
        public.mkdir(parents=True, exist_ok=True)
        shutil.move(video.path(), public / name)
        shutil.rmtree(self.out / "raw", ignore_errors=True)
        self.browser.close()
        self.pw.stop()
        if exc[0] is None:
            track = {**self.meta, "trim": max(0.0, self.caps[0]["from"] - 0.3), "end": end, "caps": self.caps}
            (self.demo / "video" / "src" / "demo.json").write_text(json.dumps(track, indent=1, ensure_ascii=False))
            print(json.dumps(self.caps, ensure_ascii=False))
