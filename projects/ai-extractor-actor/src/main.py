"""Apify actor entry point. Users bring their own Anthropic key (secret input), so AI cost is theirs;
the actor charges one 'page-extracted' event per successful page (pay-per-event)."""
import asyncio

import anthropic
import httpx
from apify import Actor

from .extractor import extract, strictify

MAX_URLS = 1000


async def main() -> None:
    async with Actor:
        inp = await Actor.get_input() or {}
        key, schema_in = inp.get("anthropicApiKey"), inp.get("schema")
        urls = [u["url"] if isinstance(u, dict) else u for u in inp.get("startUrls", [])][:MAX_URLS]
        if not key or not schema_in or not urls:
            await Actor.fail(status_message="Provide startUrls, schema and anthropicApiKey.")
            return
        try:
            schema = strictify(schema_in)
        except ValueError as e:
            await Actor.fail(status_message=f"Invalid schema: {e}")
            return

        client = anthropic.Anthropic(api_key=key)
        ok = 0
        with httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (ai-extractor actor)"}) as http:
            for i, url in enumerate(urls, 1):
                try:
                    r = await asyncio.to_thread(extract, client, http, url, schema, inp.get("instructions", ""))
                except anthropic.AuthenticationError:
                    await Actor.fail(status_message="Anthropic rejected the API key.")
                    return
                except anthropic.RateLimitError:
                    await Actor.set_status_message("Anthropic rate limit hit, waiting 30s")
                    await asyncio.sleep(30)
                    r = await asyncio.to_thread(extract, client, http, url, schema, inp.get("instructions", ""))
                row = {"url": r.url, "data": r.data, "error": r.error,
                       "inputTokens": r.input_tokens, "outputTokens": r.output_tokens}
                if r.data is not None:
                    charge = await Actor.push_data(row, charged_event_name="page-extracted")
                    ok += 1
                    if charge.event_charge_limit_reached:
                        await Actor.set_status_message(f"Spending limit reached after {ok} pages.", is_terminal=True)
                        return
                else:
                    await Actor.push_data(row)  # failed pages are listed for the user but never charged
                await Actor.set_status_message(f"{i}/{len(urls)} pages, {ok} extracted")
        await Actor.set_status_message(f"Done: {ok}/{len(urls)} pages extracted.", is_terminal=True)
