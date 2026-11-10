"""Stand-ins for the outside world, so the demo and the e2e test are repeatable and never scrape a real site:

- two competitor shops (/gadgethub, /voltmart) serving a paginated catalogue in a common e-commerce markup,
  whose prices a test can change through a token-protected admin endpoint;
- a Slack-compatible incoming webhook (/hooks/slack) that records the alerts it receives.

Not part of production: in real use the watcher points at real competitor URLs and a real Slack/Telegram.
"""
import html, json, os, secrets
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse

PER_PAGE = 10


def create_app(catalog_path: str | None = None, token: str | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)
    items = json.loads(Path(catalog_path or os.environ.get("SHOP_CATALOG", "samples/shop_catalog.json")).read_text())
    stores = {s: {i["pid"]: {"title": i["title"], "price": i[s], "in_stock": True, "listed": True} for i in items}
              for s in ("gadgethub", "voltmart")}
    token = token or os.environ["SHOP_ADMIN_TOKEN"]
    hooks: list[dict] = []
    layout = {"broken": False}  # a test can switch the markup to simulate a site redesign

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/{store}/catalogue/page-{n}.html", response_class=HTMLResponse)
    def page(store: str, n: int):
        if store not in stores:
            raise HTTPException(404)
        listed = [(pid, p) for pid, p in stores[store].items() if p["listed"]]
        chunk = listed[(n - 1) * PER_PAGE:n * PER_PAGE]
        if not chunk and n > 1:
            raise HTTPException(404)
        pod = "div" if layout["broken"] else "article"
        cards = "".join(
            f'<{pod} class="product_pod"><h3><a href="{pid}/index.html" title="{html.escape(p["title"])}">'
            f'{html.escape(p["title"])}</a></h3><div class="product_price"><p class="price_color">€{p["price"]:.2f}</p>'
            f'<p class="availability">{"In stock" if p["in_stock"] else "Out of stock"}</p></div></{pod}>'
            for pid, p in chunk)
        nxt = f'<li class="next"><a href="page-{n + 1}.html">next</a></li>' if n * PER_PAGE < len(listed) else ""
        return f"<html><body><h1>{store}</h1><ol>{cards}</ol><ul class='pager'>{nxt}</ul></body></html>"

    @app.post("/{store}/admin/set")
    def set_product(store: str, body: dict, x_token: str = Header("")):
        if not secrets.compare_digest(x_token, token):
            raise HTTPException(403)
        p = stores[store][body["pid"]]
        p.update({k: body[k] for k in ("price", "in_stock", "listed") if k in body})
        return p

    @app.post("/admin/layout")
    def set_layout(body: dict, x_token: str = Header("")):
        if not secrets.compare_digest(x_token, token):
            raise HTTPException(403)
        layout["broken"] = bool(body.get("broken"))
        return layout

    @app.post("/hooks/slack")
    async def slack(request: Request):
        hooks.append(await request.json())
        return "ok"

    @app.get("/hooks/slack")
    def slack_log():
        return hooks

    return app
