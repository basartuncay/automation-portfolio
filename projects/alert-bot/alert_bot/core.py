"""Watch RSS/Atom/JSON sources, match keywords, alert Slack or Telegram once per new item."""
import json, logging, re, time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger("alert_bot")
ATOM = "{http://www.w3.org/2005/Atom}"


@dataclass
class Item:
    id: str
    title: str
    link: str
    summary: str = ""
    source: str = ""


def parse_feed(xml_text: str, source: str = "") -> list[Item]:
    root = ET.fromstring(xml_text)
    items = []
    if root.tag == f"{ATOM}feed":
        for e in root.iter(f"{ATOM}entry"):
            link = e.find(f"{ATOM}link")
            items.append(Item(
                id=(e.findtext(f"{ATOM}id") or "").strip(), title=(e.findtext(f"{ATOM}title") or "").strip(),
                link=link.get("href", "") if link is not None else "",
                summary=(e.findtext(f"{ATOM}summary") or e.findtext(f"{ATOM}content") or "").strip(), source=source))
    else:  # RSS 2.0
        for e in root.iter("item"):
            link = (e.findtext("link") or "").strip()
            items.append(Item(id=(e.findtext("guid") or link).strip(), title=(e.findtext("title") or "").strip(),
                              link=link, summary=(e.findtext("description") or "").strip(), source=source))
    return [i for i in items if i.id]


def parse_json(data, fields: dict, source: str = "") -> list[Item]:
    """fields maps Item attributes to keys in each JSON object, e.g. {"id": "id", "title": "name", "link": "url"}."""
    rows = data
    for part in fields.get("path", "").split(".") if fields.get("path") else []:
        rows = rows[part]
    return [Item(id=str(r[fields["id"]]), title=str(r.get(fields["title"], "")), link=str(r.get(fields.get("link", ""), "")),
                 summary=str(r.get(fields.get("summary", ""), "")), source=source) for r in rows]


def matches(item: Item, include: list[str], exclude: list[str]) -> bool:
    text = f"{item.title} {item.summary}".lower()
    hit = lambda kw: re.search(rf"(?<!\w){re.escape(kw.lower())}(?!\w)", text)  # whole words: "ai" ≠ "email"
    return (not include or any(hit(k) for k in include)) and not any(hit(k) for k in exclude)


class State:
    """Remembers which item ids were already handled, per source."""

    def __init__(self, path: Path, keep: int = 2000):
        self.path, self.keep = path, keep
        self.data: dict[str, list[str]] = json.loads(path.read_text()) if path.exists() else {}

    def is_new_source(self, source: str) -> bool:
        return source not in self.data

    def unseen(self, source: str, items: list[Item]) -> list[Item]:
        seen = set(self.data.get(source, []))
        return [i for i in items if i.id not in seen]

    def mark(self, source: str, items: list[Item]) -> None:
        ids = self.data.setdefault(source, [])
        ids += [i.id for i in items if i.id not in ids]
        self.data[source] = ids[-self.keep:]  # bounded: the file never grows forever

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data))
        tmp.replace(self.path)  # atomic: a crash mid-write can't corrupt the state


def format_alert(item: Item) -> str:
    summary = re.sub(r"<[^>]+>", "", item.summary)[:280]
    return f"[{item.source}] {item.title}\n{summary}\n{item.link}".strip()


class Notifier:
    def __init__(self, client: httpx.Client, slack_webhook: str | None = None, telegram_token: str | None = None,
                 telegram_chat: str | None = None, sleep=time.sleep):
        self.client, self.slack, self.tg_token, self.tg_chat, self.sleep = client, slack_webhook, telegram_token, telegram_chat, sleep

    def _post(self, url: str, payload: dict, retries: int = 4) -> None:
        for attempt in range(retries):
            r = self.client.post(url, json=payload)
            if r.status_code == 429:
                try:
                    wait = float(r.json().get("parameters", {}).get("retry_after", 0))  # Telegram's hint
                except ValueError:
                    wait = 0
                self.sleep(wait or float(r.headers.get("Retry-After", 2 ** attempt)))
                continue
            if r.status_code >= 500:
                self.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return
        raise RuntimeError(f"notification failed after {retries} attempts")

    def send(self, text: str) -> None:
        if self.slack:
            self._post(self.slack, {"text": text})
        if self.tg_token and self.tg_chat:
            self._post(f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                       {"chat_id": self.tg_chat, "text": text, "disable_web_page_preview": True})


def run(config: dict, client: httpx.Client, notifier: Notifier, state: State, backfill: bool = False) -> int:
    sent = 0
    for src in config["sources"]:
        name = src["name"]
        try:
            r = client.get(src["url"])
            r.raise_for_status()
            items = parse_json(r.json(), src["fields"], name) if src.get("type") == "json" else parse_feed(r.text, name)
        except (httpx.HTTPError, ET.ParseError, KeyError, ValueError) as e:
            log.error("source %s failed: %s", name, e)  # one broken source must not stop the others
            continue
        new = state.unseen(name, items)
        if state.is_new_source(name) and not backfill:
            log.info("%s: first run, baselining %s existing items without alerting", name, len(new))
            state.mark(name, new)
            continue
        for item in new:
            if matches(item, src.get("include", config.get("include", [])), src.get("exclude", config.get("exclude", []))):
                notifier.send(format_alert(item))
                sent += 1
            state.mark(name, [item])  # mark after each send: a crash never re-sends earlier alerts
        state.save()
    state.save()
    return sent
