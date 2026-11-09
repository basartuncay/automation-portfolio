# JS-Rendered Site Scraper (Playwright + hidden-API discovery)

Many sites build their content with JavaScript, so a plain HTTP request gets an empty page. This project shows the two professional ways around that, and how to find the cheaper one.

| Mode | How | When |
|---|---|---|
| `browser` | Renders each page in headless Chromium (Playwright), blocks images and fonts for speed, reads the DOM and follows pagination | No usable API, or the data only exists after rendering |
| `api` | Calls the JSON endpoint the site's own frontend uses | Faster and cheaper (no browser, far less bandwidth), and more stable than CSS selectors |
| `discover` | Opens a page, scrolls it and lists every JSON endpoint it requested | Run first to find out whether `api` mode is possible |

Demo target: [quotes.toscrape.com](https://quotes.toscrape.com), a sandbox built for scraping practice. It has a JS-rendered version (`/js/`) and an infinite-scroll version (`/scroll`) backed by `/api/quotes`.

## Run it

```bash
uv sync
uv run playwright install chromium
uv run python -m js_scraper discover --url https://quotes.toscrape.com/scroll   # finds /api/quotes?page=N
uv run python -m js_scraper api                                                # all pages via the JSON API
uv run python -m js_scraper browser                                            # same data via headless Chromium
```

Output is JSON Lines (`quotes.jsonl`): `{"text": ..., "author": ..., "tags": [...]}`, deduplicated.

## Tests

```bash
uv run pytest -q                       # offline: saved rendered page + saved API response, no browser needed
uv run pytest -q -m live -o addopts=""  # real browser against the live site
```

The offline tests cover:
- parsing a rendered page
- why the raw HTML is empty
- API pagination until `has_next` is false
- API errors are raised, not swallowed
- deduplication

The live tests check that browser mode and API mode return identical data, and that `discover` finds the hidden API.

## Adapting to a client site

1. Run `discover` on the target page. If a JSON endpoint carries the data, use `api` mode with that URL.
2. Otherwise, change the selectors in `parse_rendered` and the "next page" rule in `scrape_browser`.
3. For sites with bot protection, use residential proxies and slower pacing, or the site's official API if it has one. Only collect public data that needs no login.
