"""Record a real walkthrough of the running stack for the demo video (no mock-ups). HR changes are made in the HR
stand-in, which sends its signed webhooks; the manager's approval link is read from the Slack DM the app sent.

  docker compose down -v && RUN_SECONDS=1 DRIFT_MINUTES=0 docker compose up -d --build --wait
  uv run --project ../js-scraper python demo/record.py
  cd demo/video && npx remotion render Demo ../../docs/demo.mp4 && npx remotion still Thumbnail ../../docs/demo-thumb.png
"""
import json, re, shutil, sys, time, urllib.request
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parents[2] / "tools"))
from demo_recorder import Recorder  # noqa: E402

ENV = dict(l.split("=", 1) for l in (HERE.parent / ".env").read_text().split())
APP, APIS = "http://127.0.0.1:8110", "http://127.0.0.1:8111"
H = {"x-admin": ENV["FAKES_ADMIN_TOKEN"], "Content-Type": "application/json"}
admin = lambda path, body=None: json.loads(urllib.request.urlopen(urllib.request.Request(APIS + path, json.dumps(body).encode() if body is not None else None, H)).read())
today = date.today().isoformat()
META = {"file": "accessflow.webm", "who": "Real stack · local stand-ins for the HR system, Google Workspace, Slack and GitHub", "accent": "#6366f1",
        "kicker": "Accessflow · real recording", "title": "Access that follows the HR record: joiners, movers, leavers",
        "sub": "Google Workspace, Slack and GitHub kept in line with the HR system, privileged access approved by managers, drift found, every change audited.",
        "outro": "Python · FastAPI · SQLite · Google Admin SDK · Slack SCIM · GitHub API · signed webhooks · Docker · e2e in GitHub Actions.",
        "thumbKicker": "IT AUTOMATION", "thumbTitle": "Offboarded everywhere in 2 seconds", "thumbSub": "Accessflow · HR → Google, Slack, GitHub"}

time.sleep(3)  # the first HR poll provisions the existing staff
with Recorder(HERE, META) as r:
    pg = r.page
    pg.goto(APP + "/login")
    pg.fill("input[name=email]", "it@example.com"); pg.fill("input[name=password]", "password")
    r.click(pg.locator("button.btn"), 0.5)
    r.caption("Everyone's access comes from the HR system: Google, Slack, and GitHub for engineers")
    r.pause(4)
    r.caption("HR hires Mira for Engineering, starting today. Her accounts and groups appear on their own")
    admin("/_admin/hris/created", {"id": "E010", "name": "Mira Stein", "email": "mira.stein@company.example", "personal_email": "mira@mail.example",
                                    "github": "mstein", "department": "Engineering", "title": "Platform Engineer", "manager_id": "E001", "start_date": today})
    time.sleep(2.5)
    pg.reload()
    r.pause(2)
    r.click(pg.locator("a", has_text="Mira Stein"), 4)
    r.caption("Jonas needs GitHub admin rights. IT can ask, but only his manager can approve")
    pg.goto(APP + "/people/E002")
    r.pause(1)
    r.click(pg.get_by_role("button", name=re.compile("Request")), 2)
    link = re.search(r"(http://127\.0\.0\.1:8110/approve/\S+)", admin("/_admin/state")["slack"]["dms"][-1]["text"]).group(1)
    r.caption("Hanna gets the request as a Slack message with a signed link, and approves")
    pg.goto(link)
    r.pause(1.5)
    r.click(pg.get_by_role("button", name="Approve"), 2)
    time.sleep(2)
    pg.goto(APP + "/people/E002")
    r.pause(2.5)
    r.caption("Then Jonas leaves, effective immediately. Every account, group and admin right goes within seconds")
    admin("/_admin/hris/terminated", {"id": "E002", "end_date": today, "immediate": True})
    time.sleep(3)
    pg.reload()
    r.pause(2)
    r.caption("His Drive goes to his manager, and his open sessions are ended")
    r.move_to(pg.get_by_text("transfer drive", exact=False).first)
    r.pause(3)
    r.caption("Drift: someone added a contractor as admin by hand, and a stray Slack account exists")
    admin("/_admin/manual", {"what": "github_add", "login": "old-contractor", "team": "admins"})
    admin("/_admin/manual", {"what": "slack_add", "email": "freelancer@gmail.com"})
    r.click(pg.get_by_role("link", name="Drift"), 0.5)
    r.click(pg.get_by_role("button", name="Scan now"), 3)
    r.shot("drift.png")
    r.caption("Found, and removed with one click each. All of it lands in the audit trail")
    r.click(pg.get_by_role("button", name="Remove access").first, 1.5)
    r.click(pg.get_by_role("button", name="Remove access").first, 2)
    pg.goto(APP + "/")
    r.pause(3.5)
    r.shot("people.png")
    pg.goto(APP + "/people/E002")
    r.shot("leaver.png")
for f in ("drift.png", "people.png", "leaver.png"):
    shutil.copy(HERE / "out" / f, HERE.parent / "docs" / f)
shutil.copy(HERE / "out" / "leaver.png", HERE / "video" / "public" / "queue.png")
