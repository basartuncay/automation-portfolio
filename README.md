# Automation & Integration Portfolio

Production-style automation projects: scraping, data pipelines, API integrations, AI extraction and admin panels. Each one has a README, tests and realistic sample data. All are covered by CI.

| Project | What it does | Stack | Tests |
|---|---|---|---|
| [Invoice Desk](projects/filament-ops-panel) | Accounts-payable approval panel: enforced workflow, bulk approve, dashboard, token-protected ingestion API | Laravel 13, Filament 5, Sanctum | 18 |
| [Invoice Extractor](projects/invoice-extractor) | PDF invoices → clean tables, with arithmetic checks and a review queue; CLI and HTTP API | Python, pdfplumber, FastAPI | 10 |
| [n8n Invoice Flow](projects/n8n-invoice-flow) | n8n workflow calling the custom extractor API and routing by result, verified end to end in real n8n | n8n, Python | e2e |
| [AI Email Triage](projects/email-triage) | Classify inbound email and extract fields with Claude structured outputs; injection-aware routing | Python, Claude API | 7 |
| [Document Q&A](projects/doc-qa) | Answers from company docs with sentence-level citations; says "not found" instead of guessing | Python, BM25, Claude citations | 8 |
| [Stripe Fulfillment](projects/stripe-fulfillment) | Verified webhooks → license keys, exactly-once and transactional; subscription lifecycle | FastAPI, Stripe, SQLite | 9 |
| [Shopify → Sheets](projects/shopify-sheets-sync) | Real-time order sync with HMAC verification, retry dedupe and upserts; no personal data | FastAPI, gspread | 5 |
| [Spreadsheet → Airtable ETL](projects/sheet-to-crm) | Column mapping, cleaning, validation and dedupe, then batched, rate-limited upserts | Python, httpx, openpyxl | 10 |
| [Price Monitor](projects/price-monitor) | Daily catalogue crawl with validation and price/stock change detection, scheduled on Actions | Python, httpx, selectolax | 5 |
| [JS-Site Scraper](projects/js-scraper) | Playwright rendering, or discovering and using the site's hidden JSON API | Playwright, httpx | 5 + 2 live |
| [Keyword Alert Bot](projects/alert-bot) | RSS/Atom/JSON sources → Slack/Telegram, alerting once per new item | Python, httpx | 10 |
| [Weekly KPI Report](projects/kpi-report) | Week-over-week KPIs, anomaly flags, a Sheets tab and an HTML email with a chart | Python, matplotlib | 6 |
| [AI Web Extractor](projects/ai-extractor) | Any page + JSON schema → JSON via Claude structured outputs; no selectors; hidden-text injection stripped | Python, Claude API | 8 |
| [AI Extractor Apify actor](projects/ai-extractor-actor) | The extractor as a pay-per-event Apify actor with bring-your-own-key; Docker image verified | Apify SDK, Docker | 4 |
| [Apify Niche Finder](projects/apify-niche-finder) | Market research on the Apify Store: demand vs competition per target | Python (stdlib) | self-check |

## How they fit together

```
PDF invoices ─▶ Invoice Extractor API ─▶ n8n flow ─▶ Invoice Desk (approve / pay)
                      │                                  ▲
                      └──────────── POST /api/invoices ──┘
```

The extractor's JSON output is the panel's API input, so a PDF can reach the approval queue in one pipe. See [Invoice Desk](projects/filament-ops-panel#works-with-the-rest-of-the-pipeline).

## Conventions across projects

- **Validate at the boundary.** Bad rows, bad signatures and bad totals are rejected with a reason, never silently accepted.
- **Idempotent writes.** Retries and re-runs update rather than duplicate (webhook ids, upsert keys, state files).
- **Failures are contained.** One bad file, source or email goes to a review list; the batch continues.
- **Secrets come from the environment,** never code. Webhooks are signature-verified. No personal data where it isn't needed.
- **Tests run offline:** saved pages, mocked APIs and fake LLM clients. Live tests are opt-in.

## Running the tests

```bash
cd projects/<name> && uv sync --all-extras && uv run pytest -q     # Python projects
cd projects/filament-ops-panel && ./dev.sh php artisan test         # Laravel (Docker, no local PHP needed)
```

CI (`.github/workflows/tests.yml`) runs all of the above, plus the n8n workflow end to end.
