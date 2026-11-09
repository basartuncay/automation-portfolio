# Shopify → Google Sheets Order Sync

Every new or updated Shopify order appears in a Google Sheet within seconds, as one row per order. When an order is paid or fulfilled, its row is updated in place, never duplicated.

## What it does

- **Verifies every webhook** with Shopify's HMAC signature (constant-time compare). Forged requests get a 401.
- **Handles retries:** Shopify redelivers webhooks, so each webhook id is processed once (SQLite).
- **Upserts by order id:** `orders/create`, `orders/updated`, `orders/paid` and `orders/fulfilled` all update the same row.
- **Minimises personal data:** no customer name, email, phone or street address in the sheet, only country. Add fields only if you need them.
- **Swappable output:** the `Sheet` interface has a Google Sheets implementation and an in-memory one for tests. A database, Airtable or an accounting API fits the same slot.

Columns: `order_id, order_name, created_at, financial_status, fulfillment_status, currency, subtotal, tax, total, items, item_count, ship_country`

## Run it

```bash
uv sync
export SHOPIFY_WEBHOOK_SECRET=...      # from Shopify admin > Settings > Notifications > Webhooks
export GOOGLE_SHEET_ID=...             # sheet shared with your service account
export GOOGLE_APPLICATION_CREDENTIALS=service-account.json
uv run uvicorn shopify_sync.app:app_from_env --factory --port 8000
```

Then, in Shopify admin, point the order webhooks (create, updated, paid, fulfilled, JSON format) at `https://<your-host>/webhooks/orders`. Any small host works: Railway, Fly.io, a VPS or Cloud Run.

## Tests

```bash
uv run pytest -q
```

They cover:
- a valid order becomes a row with no personal data in it
- a bad signature is rejected
- a retried webhook is processed only once
- an update overwrites the same row
- unrelated topics are ignored

## Backfill

Webhooks only see new events. To load existing orders, page through the Admin API `orders.json` once and call the same `order_to_row` → `upsert`.
