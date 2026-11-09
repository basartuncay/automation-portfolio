import argparse, json, logging, os
from pathlib import Path

import httpx

from .core import Notifier, State, run


def main() -> None:
    ap = argparse.ArgumentParser(description="Alert Slack/Telegram about new matching items in RSS/Atom/JSON sources")
    ap.add_argument("--config", type=Path, default=Path("config.json"))
    ap.add_argument("--state", type=Path, default=Path("state.json"))
    ap.add_argument("--backfill", action="store_true", help="on first run, alert on existing items too")
    ap.add_argument("--print", action="store_true", help="print alerts instead of sending (for testing)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    with httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": "alert-bot/1.0"}) as client:
        notifier = Notifier(client, os.environ.get("SLACK_WEBHOOK_URL"), os.environ.get("TELEGRAM_BOT_TOKEN"),
                            os.environ.get("TELEGRAM_CHAT_ID"))
        if args.print:
            notifier.send = lambda text: print(text, "\n---")
        sent = run(json.loads(args.config.read_text()), client, notifier, State(args.state), args.backfill)
    print(f"{sent} alerts sent")


if __name__ == "__main__":
    main()
