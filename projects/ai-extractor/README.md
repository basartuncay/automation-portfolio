# AI Web Extractor: any page + a JSON schema → clean JSON

Give it a list of URLs and a JSON schema describing the fields you want. It returns one JSON object per page that matches the schema exactly. There are no CSS selectors to write or maintain, so it keeps working when a site's layout changes and it works on sites it has never seen.

```bash
uv run python -m ai_extractor schemas/product.json \
  https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html
# OK   https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html
# 1/1 extracted, est. cost $0.0xxx -> results.jsonl
```

## How it works

1. **Fetch and clean:** the page becomes compact, readable text that keeps the structure the model needs: headings, list items, table rows, and link targets.
   - Removed: scripts, styles, navigation, footers and forms.
   - Also removed: hidden elements, the usual place to plant instructions aimed at AI.
2. **Schema hardening:** `strictify()` closes every object, requires every field, and lets optional fields be `null`, so the model can say "not on this page" instead of inventing a value.
3. **Extraction:** Claude with **structured outputs** (`output_config.format: json_schema`). The response is guaranteed to parse and match the schema.
4. **Guardrails:**
   - page text is passed as untrusted data, and instructions inside it are ignored
   - long pages are truncated before sending
   - every failure becomes an error row with the reason and never stops the batch: fetch error, malformed URL, no readable text, refusal, truncated output, API error
5. **Cost visibility:** token usage per page and an estimated total cost per run.

## Run it

```bash
uv sync
export ANTHROPIC_API_KEY=...       # or `ant auth login`
uv run python -m ai_extractor schemas/product.json URL [URL ...] --instructions "prices in EUR"
uv run python -m ai_extractor schemas/product.json @urls.txt        # one URL per line
```

`results.jsonl` holds one line per URL: `{"url", "data", "error", "input_tokens", "output_tokens"}`.

Write your own schema for job posts, real-estate listings, company "about" pages, event pages or product specs. `schemas/product.json` is the example.

## When to use this vs a classic scraper

| | Classic selectors ([price-monitor](../price-monitor)) | This |
|---|---|---|
| Cost per page | ~0 | about a cent (depends on page size) |
| Setup per site | write and maintain selectors | none |
| Layout changes | break it | usually fine |
| Best for | high volume on a few known sites | many different sites, or changing layouts |

For JS-rendered pages, fetch them with the Playwright mode from [js-scraper](../js-scraper) and pass the HTML in.

## Tests

```bash
uv run pytest -q
```

They run offline with a fake model client and cover:
- HTML cleaning on a real saved page
- hidden injection text being removed
- schema hardening
- the request shape
- the happy path and cost
- every failure type as an error row
- a malformed URL
- truncation of long pages

Model: `claude-opus-5-5`.
