"""Clients for the systems people get access to, with each API's real shape:

- Google Workspace Admin SDK: Directory users and group members, Data Transfer (Drive ownership), sign-out
- Slack SCIM v2: Users and Groups (user groups), deactivation with PATCH active=false
- GitHub REST: organization memberships and team memberships
Every operation is idempotent: "add" when already there and "remove" when already gone are both success, so a retried
action can never fail on its own earlier half-success.
"""
import os

import httpx


class Temporary(Exception):
    pass


class Refused(Exception):
    pass


def check(r: httpx.Response, ok: tuple = ()) -> httpx.Response:
    if r.status_code in ok:
        return r
    if r.status_code == 429 or r.status_code >= 500:
        raise Temporary(f"{r.status_code} from {r.request.url.path}")
    if r.status_code >= 400:
        raise Refused(f"{r.status_code}: {r.text[:200]}")
    return r


class Google:
    def __init__(self, http=None):
        self.base = os.environ["GOOGLE_URL"].rstrip("/")
        self.h = {"Authorization": "Bearer " + os.environ["GOOGLE_TOKEN"]}
        self.http = http or httpx.Client(timeout=20)

    def _u(self, path):
        return f"{self.base}/admin/directory/v1{path}"

    def ensure_user(self, email: str, name: str, recovery: str | None) -> str:
        if check(self.http.get(self._u(f"/users/{email}"), headers=self.h), ok=(404,)).status_code == 404:
            first, _, last = name.partition(" ")
            check(self.http.post(self._u("/users"), headers=self.h, json={"primaryEmail": email, "name": {"givenName": first, "familyName": last or first},
                                                                          "recoveryEmail": recovery, "changePasswordAtNextLogin": True}))
            return "created"
        check(self.http.put(self._u(f"/users/{email}"), headers=self.h, json={"suspended": False}))
        return "active"

    def suspend(self, email: str) -> str:
        r = check(self.http.put(self._u(f"/users/{email}"), headers=self.h, json={"suspended": True}), ok=(404,))
        return "not found" if r.status_code == 404 else "suspended"

    def sign_out(self, email: str) -> str:
        r = check(self.http.post(self._u(f"/users/{email}/signOut"), headers=self.h), ok=(404,))
        return "not found" if r.status_code == 404 else "sessions ended"

    def add_member(self, group: str, email: str) -> str:
        r = check(self.http.post(self._u(f"/groups/{group}/members"), headers=self.h, json={"email": email, "role": "MEMBER"}), ok=(409,))
        return "already a member" if r.status_code == 409 else "added"

    def remove_member(self, group: str, email: str) -> str:
        r = check(self.http.delete(self._u(f"/groups/{group}/members/{email}"), headers=self.h), ok=(404,))
        return "not a member" if r.status_code == 404 else "removed"

    def set_admin(self, email: str, on: bool) -> str:
        check(self.http.post(self._u(f"/users/{email}/makeAdmin"), headers=self.h, json={"status": on}))
        return "admin" if on else "not admin"

    def transfer_drive(self, from_email: str, to_email: str) -> str:
        r = check(self.http.post(f"{self.base}/admin/datatransfer/v1/transfers", headers=self.h, json={
            "oldOwnerUserId": from_email, "newOwnerUserId": to_email, "applicationDataTransfers": [{"applicationId": "drive"}]}))
        return f"transfer {r.json()['id']} to {to_email}"

    def accounts(self) -> dict[str, bool]:
        return {u["primaryEmail"]: not u["suspended"] for u in check(self.http.get(self._u("/users"), headers=self.h)).json()["users"]}


class Slack:
    def __init__(self, http=None):
        self.base = os.environ["SLACK_URL"].rstrip("/") + "/scim/v2"
        self.h = {"Authorization": "Bearer " + os.environ["SLACK_TOKEN"]}
        self.http = http or httpx.Client(timeout=20)

    def _find(self, email: str) -> dict | None:
        res = check(self.http.get(f"{self.base}/Users", params={"filter": f'userName eq "{email}"'}, headers=self.h)).json()["Resources"]
        return res[0] if res else None

    def ensure_user(self, email: str, name: str) -> str:
        u = self._find(email)
        if not u:
            check(self.http.post(f"{self.base}/Users", headers=self.h, json={"schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"], "userName": email,
                                                                           "displayName": name, "emails": [{"value": email, "primary": True}], "active": True}))
            return "created"
        if not u["active"]:
            self._active(u["id"], True)
        return "active"

    def _active(self, uid: str, on: bool):
        check(self.http.patch(f"{self.base}/Users/{uid}", headers=self.h, json={"schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                                                                              "Operations": [{"op": "replace", "value": {"active": on}}]}))

    def deactivate(self, email: str) -> str:
        u = self._find(email)
        if not u:
            return "not found"
        self._active(u["id"], False)
        return "deactivated"

    def _group(self, name: str) -> str:
        res = check(self.http.get(f"{self.base}/Groups", params={"filter": f'displayName eq "{name}"'}, headers=self.h)).json()["Resources"]
        if not res:
            raise Refused(f"no Slack user group called {name}")
        return res[0]["id"]

    def group(self, name: str, email: str, add: bool) -> str:
        u = self._find(email)
        if not u:
            if add:
                raise Temporary("user not created yet")  # the account action runs first; retry shortly
            return "no such user"
        check(self.http.patch(f"{self.base}/Groups/{self._group(name)}", headers=self.h, json={"schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
              "Operations": [{"op": "add" if add else "remove", "path": "members", "value": [{"value": u["id"]}]}]}))
        return "added" if add else "removed"

    def dm(self, email: str, text: str) -> bool:
        """A direct message through the Web API (the same bot token as SCIM in this setup)."""
        api = self.base.rsplit("/scim/v2", 1)[0] + "/api"
        u = check(self.http.get(f"{api}/users.lookupByEmail", params={"email": email}, headers=self.h)).json()
        if not u.get("ok"):
            return False
        return check(self.http.post(f"{api}/chat.postMessage", headers=self.h, json={"channel": u["user"]["id"], "text": text})).json()["ok"]

    def accounts(self) -> dict[str, bool]:
        return {u["userName"]: u["active"] for u in check(self.http.get(f"{self.base}/Users", params={"count": 1000}, headers=self.h)).json()["Resources"]}


class GitHub:
    def __init__(self, http=None):
        self.base = os.environ["GITHUB_URL"].rstrip("/")
        self.org = os.environ.get("GITHUB_ORG", "company")
        self.h = {"Authorization": "Bearer " + os.environ["GITHUB_TOKEN"], "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        self.http = http or httpx.Client(timeout=20)

    def add(self, user: str) -> str:
        check(self.http.put(f"{self.base}/orgs/{self.org}/memberships/{user}", headers=self.h, json={"role": "member"}))
        return "invited"

    def remove(self, user: str) -> str:
        r = check(self.http.delete(f"{self.base}/orgs/{self.org}/memberships/{user}", headers=self.h), ok=(404,))
        return "not a member" if r.status_code == 404 else "removed"

    def team(self, team: str, user: str, add: bool) -> str:
        url = f"{self.base}/orgs/{self.org}/teams/{team}/memberships/{user}"
        r = check(self.http.put(url, headers=self.h, json={"role": "member"}) if add else self.http.delete(url, headers=self.h), ok=(404,))
        return ("added" if add else ("not a member" if r.status_code == 404 else "removed"))

    def accounts(self) -> dict[str, bool]:
        members = check(self.http.get(f"{self.base}/orgs/{self.org}/members", headers=self.h, params={"per_page": 100})).json()
        return {m["login"]: True for m in members}

    def team_members(self, team: str) -> set[str]:
        return {m["login"] for m in check(self.http.get(f"{self.base}/orgs/{self.org}/teams/{team}/members", headers=self.h)).json()}
