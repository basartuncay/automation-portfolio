"""Clients for the outside services, with their real request shapes:

- Stripe: Checkout Sessions (form-encoded, Idempotency-Key) and webhook signatures (Stripe-Signature: t=..,v1=..)
- a project tool (Linear/Asana style): find-or-create a project by external reference, add tasks
- file storage (Drive style): find-or-create a folder, share it with the client
Every create is find-first or idempotent, so a provisioning step can be retried safely after a crash or a timeout.
"""
import hashlib, hmac, os, time

import httpx


class Temporary(Exception):
    pass


def ok(r: httpx.Response) -> dict:
    if r.status_code == 429 or r.status_code >= 500:
        raise Temporary(f"{r.status_code} from {r.request.url.host}{r.request.url.path}")
    r.raise_for_status()
    return r.json()


class Stripe:
    def __init__(self, http: httpx.Client | None = None):
        self.base = os.environ.get("STRIPE_URL", "https://api.stripe.com").rstrip("/")
        self.http = http or httpx.Client(timeout=20)
        self.key = os.environ["STRIPE_SECRET_KEY"]

    def checkout(self, *, proposal_id: int, amount_cents: int, name: str, email: str, success_url: str, cancel_url: str) -> dict:
        form = {"mode": "payment", "customer_email": email, "client_reference_id": str(proposal_id),
                "line_items[0][quantity]": "1", "line_items[0][price_data][currency]": "eur",
                "line_items[0][price_data][unit_amount]": str(amount_cents), "line_items[0][price_data][product_data][name]": name,
                "metadata[proposal_id]": str(proposal_id), "success_url": success_url, "cancel_url": cancel_url}
        return ok(self.http.post(f"{self.base}/v1/checkout/sessions", data=form, auth=(self.key, ""),
                                 headers={"Idempotency-Key": f"deposit-{proposal_id}"}))


def verify_stripe(payload: bytes, header: str, secret: str, tolerance: int = 300) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    try:
        t = int(parts.get("t", "0"))
    except ValueError:
        return False
    if abs(time.time() - t) > tolerance:  # an old, replayed event
        return False
    expected = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, v) for k, v in (p.split("=", 1) for p in header.split(",") if "=" in p) if k == "v1")


class Projects:
    def __init__(self, http: httpx.Client | None = None):
        self.base = os.environ["PROJECTS_URL"].rstrip("/")
        self.http = http or httpx.Client(timeout=20)
        self.h = {"Authorization": "Bearer " + os.environ["PROJECTS_TOKEN"]}  # per request: the client may be shared

    def ensure_project(self, ref: str, name: str, tasks: list[str]) -> dict:
        found = ok(self.http.get(f"{self.base}/v1/projects", params={"external_ref": ref}, headers=self.h))["data"]
        project = found[0] if found else ok(self.http.post(f"{self.base}/v1/projects", json={"name": name, "external_ref": ref}, headers=self.h))
        have = {t["title"] for t in ok(self.http.get(f"{self.base}/v1/projects/{project['id']}/tasks", headers=self.h))["data"]}
        for title in tasks:
            if title not in have:
                ok(self.http.post(f"{self.base}/v1/projects/{project['id']}/tasks", json={"title": title}, headers=self.h))
        return {"id": project["id"], "url": project["url"]}


class Storage:
    def __init__(self, http: httpx.Client | None = None):
        self.base = os.environ["STORAGE_URL"].rstrip("/")
        self.http = http or httpx.Client(timeout=20)
        self.h = {"Authorization": "Bearer " + os.environ["STORAGE_TOKEN"]}  # per request: the client may be shared

    def ensure_shared_folder(self, name: str, email: str) -> dict:
        found = ok(self.http.get(f"{self.base}/v1/files", params={"q": f"name = '{name}' and mimeType = 'folder'"}, headers=self.h))["files"]
        folder = found[0] if found else ok(self.http.post(f"{self.base}/v1/files", json={"name": name, "mimeType": "folder"}, headers=self.h))
        perms = ok(self.http.get(f"{self.base}/v1/files/{folder['id']}/permissions", headers=self.h))["permissions"]
        if not any(p["emailAddress"] == email for p in perms):
            ok(self.http.post(f"{self.base}/v1/files/{folder['id']}/permissions", json={"role": "writer", "type": "user", "emailAddress": email}, headers=self.h))
        return {"id": folder["id"], "url": folder["webViewLink"]}
