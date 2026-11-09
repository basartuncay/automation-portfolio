"""Shopify order webhooks -> one spreadsheet row per order (inserted on create, updated in place on update)."""
import base64, hashlib, hmac, os, sqlite3
from typing import Protocol

from fastapi import FastAPI, Header, HTTPException, Request

HEADERS = ["order_id", "order_name", "created_at", "financial_status", "fulfillment_status",
           "currency", "subtotal", "tax", "total", "items", "item_count", "ship_country"]


def order_to_row(o: dict) -> list:
    # Data minimisation: no customer name, email, phone or street address in the sheet.
    items = o.get("line_items") or []
    return [
        str(o["id"]), o.get("name"), o.get("created_at"), o.get("financial_status"),
        o.get("fulfillment_status") or "unfulfilled", o.get("currency"),
        o.get("subtotal_price"), o.get("total_tax"), o.get("total_price"),
        "; ".join(f"{i.get('quantity')}x {i.get('title')}" for i in items),
        sum(int(i.get("quantity") or 0) for i in items),
        (o.get("shipping_address") or {}).get("country_code"),
    ]


def valid_hmac(secret: str, body: bytes, header: str | None) -> bool:
    if not header:
        return False
    digest = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(digest, header)


class Sheet(Protocol):
    def upsert(self, key: str, row: list) -> None: ...


class MemorySheet:
    def __init__(self):
        self.rows: dict[str, list] = {}

    def upsert(self, key, row):
        self.rows[key] = row


class GoogleSheet:
    def __init__(self, sheet_id: str, tab: str = "orders"):
        import gspread

        ws = gspread.service_account().open_by_key(sheet_id)
        try:
            self.ws = ws.worksheet(tab)
        except gspread.WorksheetNotFound:
            self.ws = ws.add_worksheet(tab, rows=1, cols=len(HEADERS))
            self.ws.append_row(HEADERS)

    def upsert(self, key, row):
        ids = self.ws.col_values(1)
        if key in ids:
            r = ids.index(key) + 1
            self.ws.update([row], f"A{r}")
        else:
            self.ws.append_row(row, value_input_option="USER_ENTERED")


class SeenWebhooks:
    """Shopify retries deliveries; remember webhook ids so a retry is not processed twice."""

    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("create table if not exists seen (id text primary key)")

    def first_time(self, webhook_id: str) -> bool:
        with self.db:
            return self.db.execute("insert or ignore into seen values (?)", (webhook_id,)).rowcount == 1


def create_app(secret: str, sheet: Sheet, seen: SeenWebhooks) -> FastAPI:
    app = FastAPI(title="Shopify -> Sheets sync")

    @app.post("/webhooks/orders")
    async def orders(request: Request,
                     x_shopify_hmac_sha256: str | None = Header(None),
                     x_shopify_webhook_id: str | None = Header(None),
                     x_shopify_topic: str | None = Header(None)):
        body = await request.body()
        if not valid_hmac(secret, body, x_shopify_hmac_sha256):
            raise HTTPException(401, "invalid signature")
        if x_shopify_topic not in ("orders/create", "orders/updated", "orders/paid", "orders/fulfilled"):
            return {"ignored": x_shopify_topic}
        if x_shopify_webhook_id and not seen.first_time(x_shopify_webhook_id):
            return {"duplicate": True}
        order = await request.json()
        sheet.upsert(str(order["id"]), order_to_row(order))
        return {"ok": True}

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


def app_from_env() -> FastAPI:
    return create_app(os.environ["SHOPIFY_WEBHOOK_SECRET"], GoogleSheet(os.environ["GOOGLE_SHEET_ID"]),
                      SeenWebhooks(os.environ.get("SEEN_DB", "seen.sqlite")))
