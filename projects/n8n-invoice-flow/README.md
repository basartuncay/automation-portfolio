# n8n + Custom Python: Invoice Processing Flow

An importable n8n workflow that sends invoice PDFs to a custom Python extraction API and routes each invoice by whether its numbers add up. This is the usual shape of real automation work: n8n for orchestration and integrations, custom code for the part no-code can't do well.

```
Start ─▶ Read invoice PDFs ─▶ Extract (custom Python API) ─▶ Numbers add up? ─┬─ yes ─▶ To accounting
                                                                              └─ no ──▶ To human review (+ reason)
```

- **The custom step** is the [Invoice Extractor](../invoice-extractor) API: `POST /extract` with a PDF returns invoice fields, line items and a list of problems. It checks the arithmetic, handles several vendor layouts, enforces an optional API key and a 10 MB upload limit, and sanitises the filename.
- **n8n handles the rest:** triggers, file handling, branching, and whatever integrations the client uses.

## Verified end to end

`test_flow.sh` imports the workflow into a throwaway n8n home and runs it on four sample PDFs. It then decodes n8n's own execution record and asserts the routing:

```
accounting: [ [ 'acme_1.pdf', 202.78 ], [ 'nordic_1.pdf', 6751.25 ] ]
review:     [ [ 'acme_2_broken.pdf', 'subtotal + tax != total' ],
              [ 'unknown_vendor.pdf', 'unknown layout: add a template' ] ]
PASS
```

Tested with n8n 2.4.6.

## Run it

```bash
# 1. the extraction API
cd ../invoice-extractor
uv sync --extra api
uv run python make_samples.py /tmp/invoices
uv run uvicorn invoice_extractor.api:app --port 8077

# 2. the end-to-end test (separate terminal)
cd ../n8n-invoice-flow
./test_flow.sh /tmp/invoices
```

To use it in your own n8n, import `invoice-flow.json` (Workflows → Import from file), set the folder in **Read invoice PDFs**, and point **Extract** at wherever the API runs. If you set `EXTRACTOR_API_KEY`, add it as an `X-API-Key` header.

Note: use `127.0.0.1`, not `localhost`, for a local API. n8n's Node runtime resolves `localhost` to IPv6 first.

## Making it production-ready for a client

- **Trigger:** replace the manual trigger and folder read with a Gmail/Outlook attachment trigger, a Google Drive "new file" trigger, or a webhook.
- **Accounting branch:** a Google Sheets "append row", or QuickBooks/Xero "create bill".
- **Review branch:** a Slack message to the finance channel with the reason, plus a link to the file.
- **Hosting:** run the API next to n8n (same Docker network) and keep it off the public internet.
