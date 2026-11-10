# AI Schema Extractor: Apify actor

The [AI Web Extractor](../ai-extractor) packaged as a ready-to-publish Apify Store actor. Users give URLs, a JSON schema and **their own Anthropic API key**, and get one JSON row per page.

## Business model

- **Bring your own key:** users pay Anthropic directly for AI usage, so a large run can never put the actor owner into a loss.
- **Pay per event:** `page-extracted` at $0.004 (see `.actor/pay_per_event.json`). Failed pages are listed in the dataset but **never charged**.
- **Spending limit honored:** when the user's run limit is reached, the actor stops cleanly with a status message.

## Behavior

- Every page becomes a dataset row: `url, data, error, inputTokens, outputTokens`.
- A bad API key stops the run immediately with a clear message, instead of failing page by page.
- On a rate limit it waits once and retries the page.
- Invalid input (no URLs, no key, a schema that isn't an object) fails immediately with a status message.
- The key is a secret input field, encrypted by Apify and never logged.

## Publish (once the Apify account exists)

```bash
npm i -g apify-cli
apify login          # your Apify account
apify push           # builds .actor/Dockerfile on Apify
```

Then, in Apify Console: Publication → set monetization to **Pay per event** with the event in `.actor/pay_per_event.json`, and add the README to the Store listing.

## Local run

```bash
uv sync
mkdir -p storage/key_value_stores/default
echo '{"startUrls":[{"url":"https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html"}],
       "schema":{"type":"object","properties":{"name":{"type":"string"},"price":{"type":"number"}}},
       "anthropicApiKey":"sk-ant-..."}' > storage/key_value_stores/default/INPUT.json
CRAWLEE_STORAGE_DIR=storage uv run python -m src
```

## Tests

```bash
uv run pytest -q
```

They cover:
- the vendored extractor matching the library (no drift)
- success and failure rows from one run
- missing input and an invalid schema each failing with the right message, in a fresh process the way the platform runs it

The Docker image (`.actor/Dockerfile`, based on `apify/actor-python:3.12`) was built and run locally.

## Market check

Measured with [apify-niche-finder](../apify-niche-finder) on 2026-11-09: "json schema" extraction has 17 actors with about 14 monthly users in total. The closest competitor, Apify's own `ai-web-scraper`, has about 167. Competition is low, but so is demand, so treat this as a portfolio piece and a cheap Store experiment, not an income bet.
