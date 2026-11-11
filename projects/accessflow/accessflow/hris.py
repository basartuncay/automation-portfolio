"""The HR system is the source of truth. Changes arrive as signed webhooks; a periodic poll catches anything a
webhook missed. Every field change that matters for access is written to the audit log, then the person's access
is re-planned."""
import os

import httpx

from .db import audit, now
from .engine import plan

FIELDS = ("name", "email", "personal_email", "github", "department", "title", "manager_id", "start_date", "end_date", "immediate")


class HRIS:
    def __init__(self, http=None):
        self.base = os.environ["HRIS_URL"].rstrip("/")
        self.h = {"Authorization": "Bearer " + os.environ["HRIS_TOKEN"]}
        self.http = http or httpx.Client(timeout=20)

    def changed_since(self, since: str) -> list[dict]:
        r = self.http.get(f"{self.base}/v1/employees", params={"updated_since": since}, headers=self.h)
        r.raise_for_status()
        return r.json()["employees"]


def apply(db, rec: dict, source: str) -> list[str]:
    rec = {**rec, "immediate": int(bool(rec.get("immediate")))}
    old = db.execute("select * from employees where id = ?", (rec["id"],)).fetchone()
    changes = [f"{f}: {old[f]!r} → {rec.get(f)!r}" for f in FIELDS if old and old[f] != rec.get(f)]
    if old and not changes:
        return []
    db.execute(f"""insert into employees (id, {', '.join(FIELDS)}, updated_at) values (?, {', '.join('?' * len(FIELDS))}, ?)
                   on conflict(id) do update set {', '.join(f'{f} = excluded.{f}' for f in FIELDS)}, updated_at = excluded.updated_at""",
               [rec["id"]] + [rec.get(f) for f in FIELDS] + [now()])
    audit(db, rec["id"], source, "new employee record" if not old else "; ".join(changes))
    return plan(db, rec["id"])


def poll(db, hris: HRIS) -> int:
    since = (db.execute("select max(updated_at) from employees").fetchone()[0] or "2000-01-01 00:00:00")
    n = 0
    for rec in hris.changed_since(since):
        n += bool(apply(db, rec, "HR system (poll)") is not None)
    return n
