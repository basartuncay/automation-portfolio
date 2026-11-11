"""Clients for the WhatsApp Cloud API (Graph API messages endpoint) and the shop's order API, with their real
request shapes. Message builders keep WhatsApp's limits: 3 reply buttons, 20-character button titles, 24-character
list rows."""
import os

import httpx

GRAPH_VERSION = "v21.0"


class SendError(Exception):
    def __init__(self, code: int | None, message: str):
        super().__init__(message)
        self.code = code


class WhatsApp:
    def __init__(self, http: httpx.Client | None = None):
        self.base = os.environ.get("GRAPH_URL", "https://graph.facebook.com").rstrip("/")
        self.number_id = os.environ["WA_PHONE_NUMBER_ID"]
        self.h = {"Authorization": "Bearer " + os.environ["WA_ACCESS_TOKEN"]}
        self.http = http or httpx.Client(timeout=20)

    def send(self, to: str, message: dict) -> str:
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, **message}
        try:
            r = self.http.post(f"{self.base}/{GRAPH_VERSION}/{self.number_id}/messages", json=body, headers=self.h)
        except httpx.TransportError as e:
            raise SendError(None, f"network error: {e}")
        if r.status_code >= 400:
            err = (r.json() if r.headers.get("content-type", "").startswith("application/json") else {}).get("error", {})
            raise SendError(err.get("code"), err.get("message") or f"HTTP {r.status_code}")
        return r.json()["messages"][0]["id"]


def text(body: str) -> dict:
    return {"type": "text", "text": {"body": body[:4096], "preview_url": "http" in body}}


def buttons(body: str, options: list[tuple[str, str]]) -> dict:
    assert len(options) <= 3, "WhatsApp allows 3 reply buttons"
    return {"type": "interactive", "interactive": {"type": "button", "body": {"text": body[:1024]},
            "action": {"buttons": [{"type": "reply", "reply": {"id": i, "title": t[:20]}} for i, t in options]}}}


def choose(body: str, button: str, rows: list[tuple[str, str, str]]) -> dict:
    return {"type": "interactive", "interactive": {"type": "list", "body": {"text": body[:1024]}, "action": {"button": button[:20],
            "sections": [{"title": "Choose one", "rows": [{"id": i, "title": t[:24], "description": d[:72]} for i, t, d in rows[:10]]}]}}}


def template(name: str, lang: str, params: list[str]) -> dict:
    return {"type": "template", "template": {"name": name, "language": {"code": lang},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}]}}


class Shop:
    def __init__(self, http: httpx.Client | None = None):
        self.base = os.environ["SHOP_URL"].rstrip("/")
        self.h = {"Authorization": "Bearer " + os.environ["SHOP_TOKEN"]}
        self.http = http or httpx.Client(timeout=20)

    def orders_for(self, phone: str) -> list[dict]:
        r = self.http.get(f"{self.base}/v1/orders", params={"phone": phone}, headers=self.h)
        r.raise_for_status()
        return r.json()["orders"]

    def order(self, number: str) -> dict | None:
        r = self.http.get(f"{self.base}/v1/orders/{number}", headers=self.h)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def create_return(self, number: str, sku: str, reason: str, key: str) -> dict:
        r = self.http.post(f"{self.base}/v1/orders/{number}/returns", json={"sku": sku, "reason": reason}, headers={**self.h, "Idempotency-Key": key})
        r.raise_for_status()
        return r.json()
