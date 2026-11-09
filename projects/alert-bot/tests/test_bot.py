import json
from pathlib import Path

import httpx
import pytest

from alert_bot.core import Item, Notifier, State, format_alert, matches, parse_feed, parse_json, run

FIX = Path(__file__).parent / "fixtures"
RSS = (FIX / "rss.xml").read_text()


def test_parse_rss_and_atom():
    rss = parse_feed(RSS, "Demo")
    assert [i.id for i in rss] == ["g1", "g2", "g3"] and rss[0].link == "https://ex.example/1"
    atom = parse_feed((FIX / "atom.xml").read_text(), "Rel")
    assert atom[0].title == "v1.50.0" and atom[0].link.endswith("v1.50.0") and atom[0].summary == "Bug fixes"


def test_parse_json_with_nested_path():
    data = {"data": {"jobs": [{"uid": 7, "name": "Scraper needed", "href": "u"}]}}
    items = parse_json(data, {"path": "data.jobs", "id": "uid", "title": "name", "link": "href"}, "J")
    assert items == [Item("7", "Scraper needed", "u", "", "J")]


def test_keyword_matching_is_whole_word_with_excludes():
    rss = parse_feed(RSS)
    assert matches(rss[0], ["python"], [])
    assert not matches(rss[1], ["ai"], [])  # "email" must not match "ai"
    assert matches(Item("x", "AI news", ""), ["ai"], [])
    assert not matches(rss[2], ["automation"], ["hiring freeze"])
    assert matches(rss[1], [], [])  # empty include = everything


def test_alert_text_strips_html():
    assert format_alert(parse_feed(RSS, "Demo")[0]) == "[Demo] New Python scraping library released\nFast HTML parsing\nhttps://ex.example/1"


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


def feed_client(xml):
    return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=xml)))


CONFIG = {"include": ["python", "automation"], "exclude": ["hiring freeze"], "sources": [{"name": "Demo", "url": "https://f"}]}


def test_first_run_baselines_then_only_new_items_alert(tmp_path):
    state, rec = State(tmp_path / "s.json"), Recorder()
    assert run(CONFIG, feed_client(RSS), rec, state) == 0  # baseline: no spam on day one
    newer = RSS.replace("<channel><title>Demo</title>",
                        "<channel><title>Demo</title><item><title>Python automation job</title><guid>g4</guid><link>l</link></item>")
    state = State(tmp_path / "s.json")  # reload from disk like a new cron run
    assert run(CONFIG, feed_client(newer), rec, state) == 1
    assert rec.sent[0].startswith("[Demo] Python automation job")
    assert run(CONFIG, feed_client(newer), rec, State(tmp_path / "s.json")) == 0  # never twice


def test_backfill_alerts_on_existing(tmp_path):
    rec = Recorder()
    assert run(CONFIG, feed_client(RSS), rec, State(tmp_path / "s.json"), backfill=True) == 1  # g1 only; g3 excluded


def test_broken_source_does_not_stop_others(tmp_path):
    cfg = {"sources": [{"name": "Bad", "url": "https://bad"}, {"name": "Good", "url": "https://good"}]}

    def handler(req):
        return httpx.Response(500) if req.url.host == "bad" else httpx.Response(200, text=RSS)

    rec = Recorder()
    run(cfg, httpx.Client(transport=httpx.MockTransport(handler)), rec, State(tmp_path / "s.json"), backfill=True)
    assert len(rec.sent) == 3


def test_state_is_bounded(tmp_path):
    s = State(tmp_path / "s.json", keep=5)
    s.mark("x", [Item(str(i), "", "") for i in range(20)])
    s.save()
    assert json.loads((tmp_path / "s.json").read_text())["x"] == ["15", "16", "17", "18", "19"]


def test_telegram_retry_after_is_honoured():
    calls, sleeps = [], []

    def handler(req):
        calls.append(json.loads(req.content))
        if len(calls) == 1:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 7}})
        return httpx.Response(200, json={"ok": True})

    n = Notifier(httpx.Client(transport=httpx.MockTransport(handler)), telegram_token="T", telegram_chat="42", sleep=sleeps.append)
    n.send("hi")
    assert sleeps == [7.0] and calls[1] == {"chat_id": "42", "text": "hi", "disable_web_page_preview": True}


def test_slack_bad_webhook_raises():
    n = Notifier(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))), slack_webhook="https://hooks/x")
    with pytest.raises(httpx.HTTPStatusError):
        n.send("hi")
