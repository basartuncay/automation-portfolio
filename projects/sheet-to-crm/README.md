# Spreadsheet → Airtable ETL

Loads a messy CSV or Excel export into an Airtable table: maps columns, cleans and validates every value, merges duplicates, and upserts. Existing records are updated by a key field and new ones are created, so re-running never creates duplicates.

## What it does

- **Reads CSV and XLSX**, including Excel's BOM-prefixed CSV exports and skipping blank rows.
- **Maps columns through config** (`mapping.json`): source column → target field, type, required. A new client spreadsheet needs a new mapping, not new code.
- **Cleans values:**
  - `https://www.Acme.com/about` becomes `acme.com`
  - `$4,500,000` becomes `4500000`
  - `14.05.2026`, `05/14/2026` and Excel dates become `2026-05-14`
- **Rejects bad rows** to `rejects.csv` with the row number and reason (missing required value, non-numeric number, impossible date like 31/02). The rest of the file still loads.
- **Merges duplicates** in the file by the key. The last row wins, as in a sheet edited over time.
- **Upserts in batches of 10** (Airtable's limit), paced under 5 requests per second.
- **Handles failures:** retries 429 and 5xx with backoff, honoring `Retry-After`. Stops immediately on 4xx errors (wrong field name or type) instead of hammering the API.
- **Dry run:** `--dry-run` validates and shows what would be sent without sending anything.

## Run it

```bash
uv sync
uv run python -m sheet_to_crm samples/companies.csv --dry-run
# 2 valid records, 3 rejected (-> rejects.csv), 1 duplicates merged

export AIRTABLE_TOKEN=...      # personal access token with data.records:write on the base
export AIRTABLE_BASE_ID=app...
uv run python -m sheet_to_crm samples/companies.csv
# Airtable: 2 created, 0 updated   (run again: 0 created, 2 updated)
```

The Airtable table needs fields matching the `to` names in `mapping.json`. The key field (`Domain`) is what upserts merge on.

`samples/companies.csv` is synthetic company data with deliberate problems: a missing name, a non-numeric headcount, an impossible date and a duplicate company.

## Tests

```bash
uv run pytest -q
```

They cover:
- the full sample file (valid, rejected and merged rows)
- XLSX input
- every value converter
- batching (23 records → 10 + 10 + 3) and the create/update counts
- a retry after 429
- no retry on 422

All tests run offline against a mocked Airtable API.

## Other targets

The `Airtable` class is the only API-specific part. HubSpot, Salesforce, Notion, Google Sheets or a SQL database fit the same `upsert(records, key)` slot.
