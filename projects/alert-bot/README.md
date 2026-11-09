# Keyword Alert Bot (RSS / Atom / JSON → Slack or Telegram)

Watches any number of sources and sends one Slack or Telegram message per new item that matches your keywords. Typical uses: competitor news, new job posts, product releases, price-drop feeds, tender notices, brand mentions.

## What it does

- **Reads three source types:** RSS 2.0, Atom (e.g. GitHub releases) and JSON APIs, with a configurable field mapping and nested path.
- **Matches whole words:** include and exclude lists, globally or per source. `ai` matches "AI news", not "email". An exclude such as "hiring freeze" vetoes a match.
- **Alerts once:** each source's handled item ids are stored in `state.json`. The state is bounded in size and written atomically, so a crash can't corrupt it or cause a re-send.
- **No spam on day one:** the first run of a new source records existing items without alerting. Use `--backfill` to alert on them anyway.
- **Isolates failures:** one broken source is logged and skipped; the others still run.
- **Handles rate limits:** honors Telegram's `retry_after`, retries on 5xx and fails loudly on a bad Slack webhook.
- **Sends plain-text messages:** HTML is stripped from summaries and link previews are off.

## Run it

```bash
uv sync
uv run python -m alert_bot --print --backfill            # dry run: print alerts instead of sending
export SLACK_WEBHOOK_URL=https://hooks.slack.com/...       # and/or:
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
uv run python -m alert_bot
```

Schedule it every 10–15 minutes with cron, GitHub Actions or a small VPS. Each run only processes new items.

### Config (`config.json`)

```json
{
  "include": ["python", "scraping", "automation"],
  "exclude": ["hiring freeze"],
  "sources": [
    {"name": "Hacker News", "url": "https://hnrss.org/newest?points=50"},
    {"name": "Playwright releases", "url": "https://github.com/microsoft/playwright/releases.atom", "include": []},
    {"name": "Lobsters", "url": "https://lobste.rs/hottest.json", "type": "json",
     "fields": {"id": "short_id", "title": "title", "link": "url", "summary": "description"}}
  ]
}
```

A source-level `"include": []` means every new item from that source alerts.

## Tests

```bash
uv run pytest -q
```

They cover:
- RSS, Atom and nested-JSON parsing
- whole-word matching and excludes
- HTML stripping
- first-run baseline, then exactly one alert per new item across separate runs
- backfill
- a broken source not stopping the others
- the bounded state
- Telegram `retry_after`
- a bad Slack webhook

All tests run offline.
