"""Demo stand-ins with the real request shapes, so nothing touches a real directory:

- /hris    an HR system (Personio/BambooHR style): employees, changes since a time, signed webhooks on every change
- /google  Admin SDK Directory (users, group members, sign-out, admin role) and Data Transfer
- /slack   SCIM v2 Users and Groups
- /github  org memberships and team memberships
/_admin endpoints (token-protected) play HR (hire, move, terminate), an admin changing things by hand, and outages.
"""
import hashlib, hmac, json, os, secrets
from datetime import date, datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def people() -> list[dict]:
    d = lambda n: (date.today() + timedelta(days=n)).isoformat()
    base = [("E001", "Hanna Berg", "hanna.berg", "hberg-dev", "Engineering", "Head of Engineering", None, d(-900)),
            ("E002", "Jonas Klein", "jonas.klein", "jklein", "Engineering", "Backend Engineer", "E001", d(-400)),
            ("E003", "Sofia Marin", "sofia.marin", None, "Sales", "Account Executive", "E005", d(-200)),
            ("E004", "Tom Richter", "tom.richter", None, "Finance", "Accountant", "E005", d(-150)),
            ("E005", "Lea Vogel", "lea.vogel", None, "Sales", "COO", None, d(-1200))]
    return [{"id": i, "name": n, "email": f"{u}@company.example", "personal_email": f"{u.split('.')[0]}@mail.example", "github": gh, "department": dep,
             "title": t, "manager_id": m, "start_date": s, "end_date": None, "immediate": False, "updated_at": stamp()} for i, n, u, gh, dep, t, m, s in base]


def create_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)
    e = os.environ
    admin_token, hook, secret = e["FAKES_ADMIN_TOKEN"], e["HRIS_WEBHOOK_URL"], e["HRIS_WEBHOOK_SECRET"]
    tokens = {"hris": e["HRIS_TOKEN"], "google": e["GOOGLE_TOKEN"], "slack": e["SLACK_TOKEN"], "github": e["GITHUB_TOKEN"]}
    emps = {p["id"]: p for p in people()}
    g_users, g_groups, transfers, signouts = {}, {}, [], []
    s_users, s_groups = {}, {n: {"id": f"S{i}", "displayName": n, "members": set()} for i, n in enumerate(["everyone", "eng", "sales", "finance"])}
    gh_members, gh_teams = set(), {"engineering": set(), "admins": set()}
    chaos = {"slack_fail": 0}

    def auth(system: str, h: str):
        if not secrets.compare_digest(h.removeprefix("Bearer "), tokens[system]):
            raise HTTPException(401)

    def admin(x):
        if not secrets.compare_digest(x, admin_token):
            raise HTTPException(403)

    def notify(event: str, emp: dict):
        body = json.dumps({"event": event, "id": secrets.token_hex(8), "employee": emp}).encode()
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        try:
            return httpx.post(hook, content=body, headers={"Content-Type": "application/json", "X-HRIS-Signature": sig}, timeout=15).status_code
        except httpx.TransportError:
            return 0

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # ---- HR system ----
    @app.get("/hris/v1/employees")
    def changed(updated_since: str = "2000-01-01 00:00:00", authorization: str = Header("")):
        auth("hris", authorization)
        return {"employees": [p for p in emps.values() if p["updated_at"] >= updated_since]}

    @app.post("/_admin/hris/{event}")
    def hr(event: str, body: dict, x_admin: str = Header("")):
        admin(x_admin)
        emp = emps.setdefault(body["id"], {"end_date": None, "immediate": False, "github": None, "manager_id": None})
        emp.update({k: v for k, v in body.items() if k not in ("silent",)}, updated_at=stamp())
        return {"webhook": None if body.get("silent") else notify(f"employee.{event}", emp), "employee": emp}

    # ---- Google ----
    @app.get("/google/admin/directory/v1/users")
    def g_list(authorization: str = Header("")):
        auth("google", authorization)
        return {"users": list(g_users.values())}

    @app.get("/google/admin/directory/v1/users/{email}")
    def g_get(email: str, authorization: str = Header("")):
        auth("google", authorization)
        if email not in g_users:
            raise HTTPException(404)
        return g_users[email]

    @app.post("/google/admin/directory/v1/users")
    def g_create(body: dict, authorization: str = Header("")):
        auth("google", authorization)
        if body["primaryEmail"] in g_users:
            return JSONResponse({"error": {"code": 409, "message": "Entity already exists."}}, 409)
        g_users[body["primaryEmail"]] = {"primaryEmail": body["primaryEmail"], "name": body["name"], "suspended": False, "isAdmin": False}
        return g_users[body["primaryEmail"]]

    @app.put("/google/admin/directory/v1/users/{email}")
    def g_update(email: str, body: dict, authorization: str = Header("")):
        auth("google", authorization)
        if email not in g_users:
            raise HTTPException(404)
        g_users[email].update(body)
        return g_users[email]

    @app.post("/google/admin/directory/v1/users/{email}/signOut")
    def g_signout(email: str, authorization: str = Header("")):
        auth("google", authorization)
        if email not in g_users:
            raise HTTPException(404)
        signouts.append(email)
        return {}

    @app.post("/google/admin/directory/v1/users/{email}/makeAdmin")
    def g_admin(email: str, body: dict, authorization: str = Header("")):
        auth("google", authorization)
        g_users[email]["isAdmin"] = body["status"]
        return {}

    @app.post("/google/admin/directory/v1/groups/{group}/members")
    def g_add(group: str, body: dict, authorization: str = Header("")):
        auth("google", authorization)
        members = g_groups.setdefault(group, set())
        if body["email"] in members:
            return JSONResponse({"error": {"code": 409, "message": "Member already exists."}}, 409)
        members.add(body["email"])
        return {"email": body["email"], "role": "MEMBER"}

    @app.delete("/google/admin/directory/v1/groups/{group}/members/{email}")
    def g_remove(group: str, email: str, authorization: str = Header("")):
        auth("google", authorization)
        if email not in g_groups.get(group, set()):
            raise HTTPException(404)
        g_groups[group].discard(email)
        return {}

    @app.post("/google/admin/datatransfer/v1/transfers")
    def g_transfer(body: dict, authorization: str = Header("")):
        auth("google", authorization)
        t = {"id": f"T{len(transfers) + 1}", **body, "overallTransferStatusCode": "completed"}
        transfers.append(t)
        return t

    # ---- Slack SCIM ----
    def scim_filter(f: str) -> str:
        return f.split('"')[1] if '"' in f else ""

    @app.get("/slack/scim/v2/Users")
    def s_list(request: Request, authorization: str = Header("")):
        auth("slack", authorization)
        if chaos["slack_fail"] > 0:
            chaos["slack_fail"] -= 1
            raise HTTPException(503)
        f = request.query_params.get("filter")
        res = [u for u in s_users.values() if not f or u["userName"] == scim_filter(f)]
        return {"schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"], "totalResults": len(res), "Resources": res}

    @app.post("/slack/scim/v2/Users")
    def s_create(body: dict, authorization: str = Header("")):
        auth("slack", authorization)
        u = {"id": f"U{len(s_users) + 100}", "userName": body["userName"], "displayName": body["displayName"], "active": True}
        s_users[u["id"]] = u
        return JSONResponse(u, 201)

    @app.patch("/slack/scim/v2/Users/{uid}")
    def s_patch(uid: str, body: dict, authorization: str = Header("")):
        auth("slack", authorization)
        s_users[uid].update(body["Operations"][0]["value"])
        return s_users[uid]

    @app.get("/slack/scim/v2/Groups")
    def s_groups_list(request: Request, authorization: str = Header("")):
        auth("slack", authorization)
        name = scim_filter(request.query_params.get("filter", ""))
        res = [{"id": g["id"], "displayName": g["displayName"]} for g in s_groups.values() if g["displayName"] == name]
        return {"Resources": res, "totalResults": len(res)}

    @app.patch("/slack/scim/v2/Groups/{gid}")
    def s_group_patch(gid: str, body: dict, authorization: str = Header("")):
        auth("slack", authorization)
        g = next(g for g in s_groups.values() if g["id"] == gid)
        for op in body["Operations"]:
            for v in op["value"]:
                (g["members"].add if op["op"] == "add" else g["members"].discard)(v["value"])
        return {"id": gid}

    slack_dms = []

    @app.get("/slack/api/users.lookupByEmail")
    def s_lookup(email: str, authorization: str = Header("")):
        auth("slack", authorization)
        u = next((u for u in s_users.values() if u["userName"] == email and u["active"]), None)
        return {"ok": True, "user": {"id": u["id"]}} if u else {"ok": False, "error": "users_not_found"}

    @app.post("/slack/api/chat.postMessage")
    def s_post(body: dict, authorization: str = Header("")):
        auth("slack", authorization)
        slack_dms.append(body)
        return {"ok": True, "channel": body["channel"], "ts": f"{len(slack_dms)}.000"}

    # ---- GitHub ----
    @app.put("/github/orgs/{org}/memberships/{user}")
    def gh_add(org: str, user: str, authorization: str = Header("")):
        auth("github", authorization)
        gh_members.add(user)
        return {"state": "pending", "role": "member", "user": {"login": user}}

    @app.delete("/github/orgs/{org}/memberships/{user}")
    def gh_remove(org: str, user: str, authorization: str = Header("")):
        auth("github", authorization)
        if user not in gh_members:
            raise HTTPException(404)
        gh_members.discard(user)
        for t in gh_teams.values():
            t.discard(user)
        return JSONResponse(None, 204)

    @app.get("/github/orgs/{org}/members")
    def gh_list(org: str, authorization: str = Header("")):
        auth("github", authorization)
        return [{"login": m} for m in sorted(gh_members)]

    @app.put("/github/orgs/{org}/teams/{team}/memberships/{user}")
    def gh_team_add(org: str, team: str, user: str, authorization: str = Header("")):
        auth("github", authorization)
        if user not in gh_members:
            raise HTTPException(422, "user must be an org member first")
        gh_teams[team].add(user)
        return {"state": "active", "role": "member"}

    @app.delete("/github/orgs/{org}/teams/{team}/memberships/{user}")
    def gh_team_remove(org: str, team: str, user: str, authorization: str = Header("")):
        auth("github", authorization)
        if user not in gh_teams[team]:
            raise HTTPException(404)
        gh_teams[team].discard(user)
        return JSONResponse(None, 204)

    @app.get("/github/orgs/{org}/teams/{team}/members")
    def gh_team_list(org: str, team: str, authorization: str = Header("")):
        auth("github", authorization)
        return [{"login": m} for m in sorted(gh_teams[team])]

    # ---- someone changes things by hand ----
    @app.post("/_admin/manual")
    def manual(body: dict, x_admin: str = Header("")):
        admin(x_admin)
        if body["what"] == "github_add":
            gh_members.add(body["login"])
            if body.get("team"):
                gh_teams[body["team"]].add(body["login"])
        if body["what"] == "slack_add":
            s_users[f"U{len(s_users) + 100}"] = {"id": f"U{len(s_users) + 100}", "userName": body["email"], "displayName": body["email"], "active": True}
        return {"ok": True}

    @app.post("/_admin/chaos")
    def set_chaos(body: dict, x_admin: str = Header("")):
        admin(x_admin)
        chaos.update({k: int(v) for k, v in body.items()})
        return chaos

    @app.get("/_admin/state")
    def state(x_admin: str = Header("")):
        admin(x_admin)
        return {"google": {"users": g_users, "groups": {k: sorted(v) for k, v in g_groups.items()}, "transfers": transfers, "signouts": signouts},
                "slack": {"users": list(s_users.values()), "groups": {k: sorted(v["members"]) for k, v in s_groups.items()}, "dms": slack_dms},
                "github": {"members": sorted(gh_members), "teams": {k: sorted(v) for k, v in gh_teams.items()}}}

    return app
