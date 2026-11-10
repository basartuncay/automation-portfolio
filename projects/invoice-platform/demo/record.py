"""Record a real walkthrough of the running stack (no mock-ups): two browser sessions, approver then payer.
Writes demo/out/approver.webm, demo/out/payer.webm and demo/out/markers.json (scene start times in seconds).

Run with the stack up:  uv run --project ../js-scraper python demo/record.py
"""
import json, shutil, smtplib, subprocess, sys, time
from email.message import EmailMessage
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
OUT, BASE = HERE / "out", "http://127.0.0.1:8080"
VIEW = {"width": 1440, "height": 900}

# A visible cursor: Playwright videos don't show the real mouse pointer.
CURSOR = """
window.addEventListener('DOMContentLoaded', () => {
  const c = document.createElement('div');
  c.style.cssText = 'position:fixed;z-index:99999;width:22px;height:22px;border-radius:50%;pointer-events:none;' +
    'background:rgba(37,99,235,.35);border:2px solid #2563eb;transform:translate(-50%,-50%);left:-50px;top:-50px;' +
    'transition:width .12s,height .12s';
  document.body.appendChild(c);
  document.addEventListener('mousemove', e => { c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px'; });
  document.addEventListener('mousedown', () => { c.style.width = c.style.height = '14px'; });
  document.addEventListener('mouseup', () => { c.style.width = c.style.height = '22px'; });
});
"""


def db(sql: str) -> list:
    php = f'$db=new PDO("sqlite:/data/database.sqlite"); echo json_encode($db->query("{sql}")->fetchAll(PDO::FETCH_ASSOC));'
    out = subprocess.run(["docker", "compose", "exec", "-T", "panel", "php", "-r", php], cwd=HERE.parent,
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def send_emails(samples: Path) -> None:
    mails = [("billing@nordicparts.example", "Invoice NP/7781", samples / "nordic_1.pdf"),
             ("ar@acme.example", "Scanned invoice", samples / "scanned" / "acme_1_scanned.pdf"),
             ("ar@acme.example", "Invoice 1043", samples / "acme_2_broken.pdf"),
             ("hello@newvendor.example", "Our receipt", samples / "unknown_vendor.pdf"),
             ("someone@example.com", "Lunch on Friday?", None)]
    with smtplib.SMTP("127.0.0.1", 3025) as s:
        for sender, subject, f in mails:
            m = EmailMessage()
            m["From"], m["To"], m["Subject"] = sender, "ap@invoices.example", subject
            m.set_content("Hello, please see attached.")
            if f:
                m.add_attachment(f.read_bytes(), maintype="application", subtype="pdf", filename=f.name)
            s.send_message(m)


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

    def login(self, email):
        self.pg.goto(f"{BASE}/admin/login")
        self.pause(0.8)
        self.pg.fill("input[type=email]", email)  # local demo seed account
        self.pg.fill("input[type=password]", "password")
        self.click(self.pg.locator("button[type=submit]"))
        self.pg.wait_for_url("**/admin")
        self.pause(1.5)

    def row(self, number):
        return self.pg.locator("tbody tr", has_text=number).first

    def close(self):
        video = self.pg.video
        self.ctx.close()
        shutil.move(video.path(), OUT / f"{self.name}.webm")
        shutil.rmtree(OUT / f"raw-{self.name}", ignore_errors=True)
        return self.markers


def main(samples: Path) -> None:
    OUT.mkdir(exist_ok=True)
    markers = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()

        a = Session(browser, "approver")
        a.mark("login")
        a.login("approver@example.com")
        a.pg.goto(f"{BASE}/admin/invoices")
        a.pg.wait_for_selector("table")
        a.mark("before")
        a.pause(3)
        before = len(db("select id from invoices"))
        send_emails(samples)
        a.mark("emails_sent")
        for _ in range(60):  # wait for the intake worker to file them
            if len(db("select id from invoices")) >= before + 4:
                break
            time.sleep(1)
        a.pg.reload()
        a.pg.wait_for_selector("table")
        a.mark("arrived")
        a.pause(4)
        a.mark("ocr_invoice")
        a.click(a.row("INV-01042").locator("a").first, pause=1.5)
        a.pg.mouse.wheel(0, 500)
        a.pause(3.5)
        a.pg.goto(f"{BASE}/admin/invoices")
        a.pg.wait_for_selector("table")
        a.pause(1)
        a.mark("approve")
        a.click(a.row("INV-01042").get_by_role("button", name="Approve"), pause=1.2)
        a.click(a.pg.locator(".fi-modal-window:visible").get_by_role("button", name="Confirm"), pause=2.5)
        a.mark("no_pay_for_approver")
        a.row("INV-01042").hover()
        a.pause(3.5)
        markers["approver"] = a.close()

        p_ = Session(browser, "payer")
        p_.mark("payer_login")
        p_.login("payer@example.com")
        p_.pg.goto(f"{BASE}/admin/invoices")
        p_.pg.wait_for_selector("table")
        p_.pause(1.2)
        p_.mark("pay")
        p_.click(p_.row("INV-01042").get_by_role("button", name="Mark paid"), pause=1.2)
        p_.click(p_.pg.locator(".fi-modal-window:visible").get_by_role("button", name="Confirm"), pause=2.5)
        p_.mark("history")
        p_.click(p_.row("INV-01042").locator("a").first, pause=1.2)
        tab = p_.pg.get_by_role("tab", name="History")
        p_.click(tab.first, pause=1)
        p_.pg.mouse.wheel(0, 900)
        p_.pause(5)
        markers["payer"] = p_.close()
        browser.close()

    (OUT / "markers.json").write_text(json.dumps(markers, indent=1))
    paid = db("select status from invoices where number = 'INV-01042'")
    assert paid and paid[0]["status"] == "paid", paid  # the video shows real state changes
    print(json.dumps(markers, indent=1))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
