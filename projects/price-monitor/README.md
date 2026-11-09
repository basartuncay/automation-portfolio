# Price Monitor

A scheduled scraper that crawls a product catalogue every day, validates every row and reports what changed since the last run: new products, removed products, price changes and stock changes. Results go to CSV/JSON and, optionally, to Google Sheets.

The demo targets [books.toscrape.com](https://books.toscrape.com), a sandbox built for scraping practice. Pointing it at a real catalogue means changing the selectors in `parse_list`.

## What it does

- **Crawls every page** with polite delays and retries with backoff on 429/5xx and network errors (no retry on 4xx)
- **Validates each row** (title present, price > 0); bad rows go to `errors.log` instead of crashing the run
- **Deduplicates** products across pages
- **Detects changes** against the previous snapshot: `new`, `removed`, `price`, `stock`
- **Fails loudly** when fewer rows than expected come back (`--min-products`), which is the usual sign the site layout changed
- **Runs daily** on GitHub Actions and keeps the last snapshot between runs; tests run before every crawl

## Run it

```bash
uv sync
uv run python -m price_monitor --max-pages 5 --out data
```

Output in `data/`:

| File | Content |
|---|---|
| `products.csv` | id, title, price, currency, rating, in_stock, url |
| `changes.csv` | id, change (new/removed/price/stock), old, new, title |
| `snapshot.json` | state used for the next diff |
| `errors.log` | rows rejected by validation |

### Google Sheets (optional)

```bash
uv sync --extra sheets
export GOOGLE_SHEET_ID=<sheet id>
export GOOGLE_APPLICATION_CREDENTIALS=service-account.json
uv run python -m price_monitor
```

Share the sheet with the service account's email. The run writes a `products` and a `changes` tab.

## Tests

```bash
uv run pytest -q
```

They cover parsing a real saved page, rejecting bad rows, every change type, retrying on 503 and not retrying on 404.

## Scope and ethics

Collects only public data that needs no login and contains no personal data, at a polite request rate.
