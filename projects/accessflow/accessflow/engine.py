"""Desired state, like infrastructure as code but for people.

For each employee, the access they *should* have follows from the HR record: nothing before the day before they
start, their department's access while employed, nothing after their last day (or right away for an immediate
termination). Privileged access (GitHub admins, Google admin) is never derived: it's requested and needs a manager's
approval.

`plan` compares that with what has been granted and queues the difference as actions. `run` executes them against
the systems with retries. Every action is idempotent, so the loop can run as often as it likes; a leaver is also
signed out everywhere and their Drive files go to their manager.
"""
import json, time
from datetime import date, timedelta

from .db import PRIVILEGED, RULES, audit, now
from .targets import Refused, Temporary

ORDER = {"create_account": 0, "add": 1, "remove": 2, "transfer_drive": 3, "sign_out": 4, "suspend_account": 5}


def phase(emp, today: date | None = None) -> str:
    today = today or date.today()
    if emp["end_date"] and (emp["immediate"] or date.fromisoformat(emp["end_date"]) < today):
        return "left"
    if date.fromisoformat(emp["start_date"]) - timedelta(days=1) > today:
        return "not yet"  # accounts are ready the day before the first day, not weeks earlier
    return "leaving" if emp["end_date"] else "active"


def wanted(db, emp, today: date | None = None) -> set[tuple[str, str]]:
    if phase(emp, today) in ("left", "not yet"):
        return set()
    out = set(RULES["*"]) | set(RULES.get(emp["department"], []))
    if emp["github"] is None:
        out = {w for w in out if w[0] != "github"}
    approved = db.execute("select system, resource from grants where employee_id = ? and privileged = 1 and state in ('granted', 'awaiting_approval') and approved_by is not null",
                          (emp["id"],)).fetchall()
    return out | {(r["system"], r["resource"]) for r in approved}


def queue(db, emp_id: str, system: str, op: str, target: str, detail: str = "") -> bool:
    if db.execute("select 1 from actions where employee_id = ? and system = ? and op = ? and target = ? and status = 'pending'",
                  (emp_id, system, op, target)).fetchone():
        return False
    db.execute("insert into actions (employee_id, system, op, target, created_at, detail) values (?, ?, ?, ?, ?, ?)",
               (emp_id, system, op, target, now(), detail))
    return True


def plan(db, emp_id: str, today: date | None = None) -> list[str]:
    emp = db.execute("select * from employees where id = ?", (emp_id,)).fetchone()
    want = wanted(db, emp, today)
    have = {(g["system"], g["resource"]) for g in db.execute("select * from grants where employee_id = ? and state = 'granted'", (emp_id,))}
    pending = {(a["system"], a["target"]) for a in db.execute("select * from actions where employee_id = ? and status = 'pending'", (emp_id,))}
    steps = []
    for system, res in sorted(want - have, key=lambda w: (w[1] != "account", w)):
        if (system, res) in pending:
            continue
        steps.append((system, "create_account" if res == "account" else "add", res))
    leaving_now = phase(emp, today) == "left"
    for system, res in sorted(have - want, key=lambda w: (w[1] == "account", w)):
        if (system, res) in pending:
            continue
        if res == "account" and system == "google" and leaving_now and emp["manager_id"]:
            steps += [("google", "transfer_drive", res), ("google", "sign_out", res)]
        steps.append((system, "suspend_account" if res == "account" else "remove", res))
    queued = [f"{s} {op} {t}" for s, op, t in steps if queue(db, emp_id, s, op, t)]
    if queued:
        audit(db, emp_id, "planner", "; ".join(queued))
    return queued


def plan_all(db, today: date | None = None) -> int:
    return sum(len(plan(db, e["id"], today)) for e in db.execute("select id from employees").fetchall())


def execute(db, a, emp, clients) -> str:
    g, s, gh = clients["google"], clients["slack"], clients["github"]
    res = a["target"]
    kind, _, name = res.partition(":")
    if a["system"] == "google":
        if a["op"] == "create_account":
            return g.ensure_user(emp["email"], emp["name"], emp["personal_email"])
        if a["op"] == "suspend_account":
            return g.suspend(emp["email"])
        if a["op"] == "sign_out":
            return g.sign_out(emp["email"])
        if a["op"] == "transfer_drive":
            mgr = db.execute("select email from employees where id = ?", (emp["manager_id"],)).fetchone()
            return g.transfer_drive(emp["email"], mgr["email"])
        if kind == "group":
            return g.add_member(name, emp["email"]) if a["op"] == "add" else g.remove_member(name, emp["email"])
        if kind == "role":
            return g.set_admin(emp["email"], a["op"] == "add")
    if a["system"] == "slack":
        if a["op"] == "create_account":
            return s.ensure_user(emp["email"], emp["name"])
        if a["op"] == "suspend_account":
            return s.deactivate(emp["email"])
        return s.group(name, emp["email"], a["op"] == "add")
    if a["system"] == "github":
        if a["op"] == "create_account":
            return gh.add(emp["github"])
        if a["op"] == "suspend_account":
            return gh.remove(emp["github"])
        return gh.team(name, emp["github"], a["op"] == "add")
    raise Refused(f"don't know how to {a['op']} {res} in {a['system']}")


def run(db, clients, max_attempts: int = 6) -> dict:
    done = failed = 0
    blocked = set()  # (employee, system) with an unfinished earlier step: later steps wait, so groups never precede accounts
    due = db.execute("select * from actions where status = 'pending' order by employee_id, system, id").fetchall()
    for a in sorted(due, key=lambda a: (a["employee_id"], a["system"], ORDER.get(a["op"], 9), a["id"])):
        key = (a["employee_id"], a["system"])
        if key in blocked or a["next_at"] > time.time():
            blocked.add(key)
            continue
        emp = db.execute("select * from employees where id = ?", (a["employee_id"],)).fetchone()
        try:
            result = execute(db, a, emp, clients)
        except (Temporary, OSError) as e:
            n = a["attempts"] + 1
            db.execute("update actions set attempts = ?, error = ?, next_at = ?, status = ? where id = ?",
                       (n, str(e)[:300], time.time() + min(2 ** n, 300) * 0.5, "failed" if n >= max_attempts else "pending", a["id"]))
            blocked.add(key)
            failed += n >= max_attempts
            continue
        except Refused as e:
            db.execute("update actions set attempts = attempts + 1, error = ?, status = 'failed' where id = ?", (str(e)[:300], a["id"]))
            audit(db, a["employee_id"], "system", f"FAILED {a['system']} {a['op']} {a['target']}: {e}")
            blocked.add(key)
            failed += 1
            continue
        db.execute("update actions set status = 'done', done_at = ?, attempts = attempts + 1, error = null, detail = ? where id = ?", (now(), result, a["id"]))
        if a["op"] in ("create_account", "add"):
            db.execute("""insert into grants (employee_id, system, resource, privileged, state, requested_at, granted_at) values (?, ?, ?, ?, 'granted', ?, ?)
                          on conflict(employee_id, system, resource) do update set state = 'granted', granted_at = excluded.granted_at""",
                       (a["employee_id"], a["system"], a["target"], int((a["system"], a["target"]) in PRIVILEGED), now(), now()))
        elif a["op"] in ("remove", "suspend_account"):
            db.execute("update grants set state = 'removed', removed_at = ? where employee_id = ? and system = ? and resource = ?",
                       (now(), a["employee_id"], a["system"], a["target"]))
        audit(db, a["employee_id"], "system", f"{a['system']} {a['op'].replace('_', ' ')} {a['target']}: {result}")
        done += 1
    return {"done": done, "failed": failed}


# ---- privileged access ----

def request_privileged(db, emp_id: str, system: str, resource: str, who: str) -> None:
    if (system, resource) not in PRIVILEGED:
        raise ValueError("Only privileged access is requested by hand; everything else follows the HR record.")
    db.execute("""insert into grants (employee_id, system, resource, privileged, state, requested_at) values (?, ?, ?, 1, 'awaiting_approval', ?)
                  on conflict(employee_id, system, resource) do update set state = 'awaiting_approval', approved_by = null, requested_at = excluded.requested_at""",
               (emp_id, system, resource, now()))
    audit(db, emp_id, who, f"requested {system} {resource}")


def decide(db, grant_id: int, approver_email: str, approve: bool) -> str:
    g = db.execute("select g.*, e.manager_id from grants g join employees e on e.id = g.employee_id where g.id = ?", (grant_id,)).fetchone()
    mgr = db.execute("select email from employees where id = ?", (g["manager_id"],)).fetchone() if g["manager_id"] else None
    if not mgr or mgr["email"] != approver_email:
        raise PermissionError("Only the employee's manager can approve privileged access.")
    if approve:
        db.execute("update grants set approved_by = ? where id = ?", (approver_email, grant_id))
        audit(db, g["employee_id"], approver_email, f"approved {g['system']} {g['resource']}")
        plan(db, g["employee_id"])
        return "approved"
    db.execute("update grants set state = 'denied', approved_by = ? where id = ?", (approver_email, grant_id))
    audit(db, g["employee_id"], approver_email, f"denied {g['system']} {g['resource']}")
    return "denied"


# ---- drift: what exists that nobody planned ----

def scan(db, clients) -> list[dict]:
    """Accounts in the systems that don't belong to anyone who should have one, and admin access nobody approved."""
    emps = db.execute("select * from employees").fetchall()
    should = {"google": {e["email"] for e in emps if wanted(db, e) & {("google", "account")}},
              "slack": {e["email"] for e in emps if wanted(db, e) & {("slack", "account")}},
              "github": {e["github"] for e in emps if e["github"] and wanted(db, e) & {("github", "account")}}}
    found = []
    for system in ("google", "slack", "github"):
        for account, active in clients[system].accounts().items():
            if active and account not in should[system] and not account.startswith("svc-") and not account.endswith("-bot"):
                found.append({"system": system, "account": account, "detail": "active account with no current employee behind it"})
    admins = {e["github"] for e in emps if e["github"] and ("github", "team:admins") in wanted(db, e)}
    for login in clients["github"].team_members("admins") - admins:
        found.append({"system": "github", "account": login, "detail": "in the admins team without an approval"})
    seen = set()
    for f in found:
        seen.add((f["system"], f["account"]))
        if db.execute("insert or ignore into drift (system, account, detail, found_at) values (?, ?, ?, ?)", (f["system"], f["account"], f["detail"], now())).rowcount:
            audit(db, None, "drift scan", f"{f['system']} {f['account']}: {f['detail']}")
    for d in db.execute("select * from drift where status = 'open'").fetchall():  # fixed meanwhile
        if (d["system"], d["account"]) not in seen:
            db.execute("update drift set status = 'resolved', resolved_by = 'no longer found', resolved_at = ? where id = ?", (now(), d["id"]))
    return found
