# Automation & Integration Portfolio

Production-style automation projects: scraping, data pipelines, API integrations, AI extraction and admin panels. Each one has a README, tests and realistic sample data. All are covered by CI.

| Project | What it does | Stack | Tests |
|---|---|---|---|
| **[Invoice Platform](projects/invoice-platform)** ★ | **Flagship.** Supplier email → IMAP worker → extraction with OCR → approval panel, one `docker compose up`; no-loss retries, four-eyes payments, audit trail; verified end to end in CI; [1-min demo video](projects/invoice-platform/docs/demo.mp4) | Docker Compose, Laravel, Python, Tesseract | 8 + e2e |
| **[Ops Agent](projects/ops-agent)** ★ | **AI agent flagship.** Back-office tasks in plain words → Claude tool use over orders, refunds, stock → limits enforced in code, manager approvals that survive restarts, full traces, 14 state-checking evals, and the same tools as an MCP server; [70-sec demo video](projects/ops-agent/docs/demo.mp4) | Claude API, MCP, FastAPI, Docker | 12 + e2e |
| **[Sync Hub](projects/sync-hub)** ★ | **Integration flagship.** Two-way store ⇄ CRM customer sync: OAuth with refresh, HMAC webhooks, field ownership, conflicts sent to a person, no echo loops, retries and a dead-letter queue with replay; [68-sec demo video](projects/sync-hub/docs/demo.mp4) | OAuth 2, webhooks, FastAPI, Docker | 14 + e2e |
| **[Revenue Pipeline](projects/revenue-pipeline)** ★ | **Data flagship.** Shopify + Stripe + Google/Meta ads → incremental loads → dbt models and 20 data tests on DuckDB → dashboard that only shows builds that passed (ROAS, CAC, cohorts); late refunds, schema drift and bad rows handled; [64-sec demo video](projects/revenue-pipeline/docs/demo.mp4) | dbt, DuckDB, FastAPI, Docker | 8 + e2e |
| **[Stock Sync](projects/stock-sync)** ★ | **E-commerce flagship.** One stock ledger for Shopify, Amazon (SP-API) and the warehouse: bundles from their parts, per-channel safety buffers, sell-outs reach the other channel in under a second, all-or-nothing order reservations, oversells caught as exceptions, two-run reconciliation; [55-sec demo video](projects/stock-sync/docs/demo.mp4) | Shopify, Amazon SP-API, FastAPI, Docker | 8 + e2e |
| **[Paymatch](projects/paymatch)** ★ | **Finance flagship.** CAMT.053 / bank CSV → payments matched to open invoices with explained scores (discount, bank fees, part and multi-invoice payments) → booked in accounting only when sure → review queue → reminders only for what's really overdue; [48-sec demo video](projects/paymatch/docs/demo.mp4) | CAMT.053, FastAPI, Docker | 6 + e2e |
| **[Lead Engine](projects/lead-engine)** ★ | **Outbound flagship.** Apollo/Sales Navigator exports merged by email → verified, enriched, scored with reasons → 3-step threaded sequence in a send window → IMAP replies sorted (interested, out of office, unsubscribe, bounce) → interested people to the CRM; [62-sec demo video](projects/lead-engine/docs/demo.mp4) | SMTP/IMAP, HubSpot API, FastAPI, Docker | 8 + e2e |
| **[Kickoff](projects/kickoff)** ★ | **Client onboarding flagship.** Enquiry → proposal → typed e-signature bound to the document's SHA-256, signed PDF → Stripe Checkout deposit (verified, replay-safe webhooks) → project, shared folder and welcome email set up automatically; [1-min demo video](projects/kickoff/docs/demo.mp4) | Stripe, fpdf2, FastAPI, Docker | 9 + e2e |
| **[Chatline](projects/chatline)** ★ | **Messaging flagship.** WhatsApp Cloud API customer service: order tracking and returns by buttons and lists, handoff to a team inbox with the bot silent, the 24-hour window, opt-in templates, signed and de-duplicated webhooks; [65-sec demo video](projects/chatline/docs/demo.mp4) | WhatsApp Cloud API, FastAPI, Docker | 8 + e2e |
| **[Accessflow](projects/accessflow)** ★ | **IT automation flagship.** Joiners, movers and leavers from the HR system to Google Workspace, Slack and GitHub as desired state: day-before provisioning, immediate offboarding in ~2 s with Drive handed to the manager, manager-approved admin access, drift scans, audit CSV; [58-sec demo video](projects/accessflow/docs/demo.mp4) | Google Admin SDK, Slack SCIM, GitHub API, Docker | 9 + e2e |
| **[Meter](projects/meter)** ★ | **SaaS billing flagship.** Usage-based billing on Stripe: events counted once and reported to Stripe's meter with reconciliation, included usage then overage, 80%/100% alerts and spend caps, prorated upgrades, failed payments with a grace period then restriction; [63-sec demo video](projects/meter/docs/demo.mp4) | Stripe Billing, FastAPI, Docker | 7 + e2e |
| **[Table Desk](projects/table-desk)** ★ | **Local business flagship.** Restaurant reservations from an AI phone agent, a booking page, SMS and the staff panel through one engine: table joins, no double bookings, SMS confirm/cancel (STORNO), waitlist offers, reminders, data retention; [57-sec demo video](projects/table-desk/docs/demo.mp4) | Voice-agent tools, Twilio API, FastAPI, Docker | 14 + e2e |
| **[Support Desk](projects/support-desk)** ★ | **AI flagship.** Support inbox → triage → reply drafted from your docs with sources → agent/lead approves and sends in thread; injection-aware, exactly-once send, knowledge-gap list; [1-min demo video](projects/support-desk/docs/demo.mp4) | Docker Compose, FastAPI, Claude API | 12 + e2e |
| **[Price Watch](projects/price-watch)** ★ | **Scraping flagship.** Scheduled competitor crawls → price history → undercut/drop/stock alerts to Slack or Telegram → weekly digest email → panel with charts; catches site redesigns instead of reporting false removals; [1-min demo video](projects/price-watch/docs/demo.mp4) | Docker Compose, httpx, FastAPI, Slack | 9 + e2e |
| [Invoice Desk](projects/filament-ops-panel) | Accounts-payable approval panel: roles, four-eyes rule, audit trail, bulk approve, dashboard, token-protected ingestion API; production FrankenPHP image | Laravel 13, Filament 5, Sanctum | 26 |
| [Invoice Extractor](projects/invoice-extractor) | PDF invoices (text or scanned, via OCR) → clean tables, with arithmetic checks and a review queue; CLI and HTTP API | Python, pdfplumber, Tesseract, FastAPI | 14 |
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
supplier email ─▶ intake worker ─▶ Invoice Extractor API (text + OCR) ─▶ Invoice Desk (approve / pay)
                                        ▲                                      ▲
PDF folder ─────▶ n8n flow ─────────────┘──────────── POST /api/invoices ──────┘
```

The [Invoice Platform](projects/invoice-platform) runs the email path as one Docker Compose stack.

```
customer email ─▶ Support Desk worker ─▶ AI Email Triage ─▶ Document Q&A retrieval ─▶ cited draft ─▶ human approves ─▶ reply
```

The [Support Desk](projects/support-desk) reuses the triage and Q&A projects as libraries.

```
competitor sites ─▶ Price Watch watcher ─▶ Price Monitor crawl + diff ─▶ history + alerts ─▶ Alert Bot (Slack/Telegram) + digest
```

The [Price Watch](projects/price-watch) does the same with the price monitor and the alert bot.

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

CI (`.github/workflows/tests.yml`) runs all of the above, plus the n8n workflow and all fourteen flagship stacks end to end in Docker.
