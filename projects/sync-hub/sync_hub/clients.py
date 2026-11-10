"""API clients for the two systems. Every failure is classified once, here, so the engine only decides retry or not:

- Retryable: 429 (honouring Retry-After), 5xx, timeouts, network errors
- Permanent: other 4xx (bad data, not found, duplicate); retrying won't help, the event goes to the dead-letter list
- An expired CRM access token is refreshed once, transparently, and the request repeated
"""
import os, time

import httpx

from . import db as store


class Retryable(Exception):
    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


class Permanent(Exception):
    pass


def check(r: httpx.Response, what: str) -> dict:
    if r.status_code == 429:
        raise Retryable(f"{what}: rate limited", float(r.headers.get("Retry-After", 5)))
    if r.status_code >= 500:
        raise Retryable(f"{what}: {r.status_code} from the server")
    if r.status_code >= 400:
        raise Permanent(f"{what}: {r.status_code} {r.text[:200]}")
    return r.json()


class CRM:
    """HubSpot-style contacts API behind OAuth 2 (authorization code + rotating refresh tokens)."""

    def __init__(self, conn, http: httpx.Client, base: str | None = None):
        self.conn, self.http = conn, http
        self.base = (base or os.environ["CRM_URL"]).rstrip("/")
        self.client_id, self.client_secret = os.environ.get("CRM_CLIENT_ID", "sync-hub"), os.environ["CRM_CLIENT_SECRET"]

    # --- OAuth ---
    def authorize_url(self, redirect_uri: str, state: str, public_base: str | None = None) -> str:
        base = (public_base or os.environ.get("CRM_PUBLIC_URL") or self.base).rstrip("/")
        return f"{base}/oauth/authorize?" + str(httpx.QueryParams(client_id=self.client_id, redirect_uri=redirect_uri, state=state))

    def exchange(self, code: str, redirect_uri: str) -> None:
        self._store(self._token({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}))

    def _token(self, form: dict) -> dict:
        try:
            r = self.http.post(f"{self.base}/oauth/token", data=form | {"client_id": self.client_id, "client_secret": self.client_secret})
        except httpx.HTTPError as e:
            raise Retryable(f"CRM token endpoint unreachable: {e}")
        if r.status_code >= 500:
            raise Retryable(f"CRM token endpoint: {r.status_code}")
        if r.status_code >= 400:
            self.conn.execute("update connections set status = 'needs_reconnect', last_error = ? where name = 'crm'", (r.text[:200],))
            self.conn.commit()
            raise Permanent("CRM rejected the refresh token: reconnect the CRM in the panel")
        return r.json()

    def _store(self, t: dict) -> None:
        self.conn.execute("""insert into connections (name, status, access_token, refresh_token, expires_at, connected_at)
                             values ('crm', 'connected', ?, ?, ?, ?) on conflict(name) do update set status = 'connected',
                             access_token = excluded.access_token, refresh_token = excluded.refresh_token,
                             expires_at = excluded.expires_at, last_error = null""",
                          (store.seal(t["access_token"]), store.seal(t["refresh_token"]), time.time() + t["expires_in"], store.now()))
        self.conn.commit()

    def refresh(self) -> None:
        row = self.conn.execute("select refresh_token from connections where name = 'crm'").fetchone()
        self._store(self._token({"grant_type": "refresh_token", "refresh_token": store.unseal(row["refresh_token"])}))

    def connected(self) -> bool:
        row = self.conn.execute("select status from connections where name = 'crm'").fetchone()
        return bool(row and row["status"] == "connected")

    def _req(self, method: str, path: str, **kw) -> dict:
        row = self.conn.execute("select * from connections where name = 'crm'").fetchone()
        if not row or row["status"] != "connected":
            raise Retryable("CRM not connected yet", 30)
        if row["expires_at"] < time.time() + 30:
            self.refresh()
        for attempt in (1, 2):
            token = store.unseal(self.conn.execute("select access_token from connections where name = 'crm'").fetchone()[0])
            try:
                r = self.http.request(method, f"{self.base}{path}", headers={"Authorization": f"Bearer {token}"}, **kw)
            except httpx.HTTPError as e:
                raise Retryable(f"CRM unreachable: {type(e).__name__}")
            if r.status_code == 401 and attempt == 1:  # revoked or expired early: refresh once, then repeat
                self.refresh()
                continue
            return check(r, f"CRM {method} {path}")

    def list_updated(self, after: str) -> list[dict]:
        return self._req("GET", "/v3/objects/contacts", params={"updatedAfter": after, "limit": 100})["results"]

    def get(self, cid: str) -> dict:
        return self._req("GET", f"/v3/objects/contacts/{cid}")

    def find_by_email(self, email: str) -> dict | None:
        res = self._req("GET", "/v3/objects/contacts", params={"email": email, "limit": 1})["results"]
        return res[0] if res else None

    def create(self, props: dict) -> dict:
        return self._req("POST", "/v3/objects/contacts", json={"properties": props})

    def update(self, cid: str, props: dict) -> dict:
        return self._req("PATCH", f"/v3/objects/contacts/{cid}", json={"properties": props})


class Shop:
    """Shopify-style Admin API with a private-app access token."""

    def __init__(self, http: httpx.Client, base: str | None = None, token: str | None = None):
        self.http = http
        self.base = (base or os.environ["SHOP_URL"]).rstrip("/") + "/admin/api/2026-10"
        self.token = token or os.environ["SHOP_ACCESS_TOKEN"]

    def _req(self, method: str, path: str, **kw) -> dict:
        try:
            r = self.http.request(method, f"{self.base}{path}", headers={"X-Shopify-Access-Token": self.token}, **kw)
        except httpx.HTTPError as e:
            raise Retryable(f"Shop unreachable: {type(e).__name__}")
        return check(r, f"Shop {method} {path}")

    def get(self, cid: str) -> dict:
        return self._req("GET", f"/customers/{cid}.json")["customer"]

    def all(self) -> list[dict]:
        return self._req("GET", "/customers.json")["customers"]

    def find_by_email(self, email: str) -> dict | None:
        return next((c for c in self.all() if (c.get("email") or "").lower() == email.lower()), None)

    def create(self, fields: dict) -> dict:
        return self._req("POST", "/customers.json", json={"customer": fields})["customer"]

    def update(self, cid: str, fields: dict) -> dict:
        return self._req("PUT", f"/customers/{cid}.json", json={"customer": fields})["customer"]
