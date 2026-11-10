"""Stand-ins for the three sources, with the request shapes of the real ones, so the demo and the e2e test are
repeatable and touch no real account:

- /shop    Shopify-style orders API: updated_at_min filter, Link-header cursor pagination
- /stripe  Stripe-style charges and refunds: created[gte] filter, starting_after pagination, amounts in cents
- /ads     Google Ads and Meta Ads daily exports as CSV (each platform with its own column names)

60 days of deterministic data. /_admin endpoints (token-protected) inject the things that go wrong in real life:
late refunds, duplicated records, a negative spend row, a renamed CSV column, outages. /_admin/truth computes the
expected numbers independently of the pipeline, for the e2e test.
"""
import csv, io, os, random, secrets
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, Header, HTTPException, Request, Response

START, DAYS = date(2026, 9, 1), 60
CHANNELS = {"google": 0.36, "meta": 0.30, "newsletter": 0.14, "direct": 0.20}
PRODUCTS = [("Wireless Headphones X200", 128.99), ("Noise-Cancelling Earbuds Pro", 88.99), ("USB-C Charger 65W", 38.99),
            ("Mechanical Keyboard K87", 98.99), ("Portable SSD 1TB", 108.99), ("Webcam 1080p", 58.99), ("Power Bank 20000mAh", 41.99)]


def ts(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def generate(seed: int = 7) -> dict:
    rnd = random.Random(seed)
    orders, charges, refunds, ads = [], [], [], []
    customers: list[int] = []
    oid, cid_next = 5001, 1
    for d in range(DAYS):
        day = datetime.combine(START + timedelta(days=d), datetime.min.time(), tzinfo=timezone.utc)
        weekend = day.weekday() >= 5
        n = rnd.randint(9, 16) + (4 if weekend else 0) + d // 12
        for _ in range(n):
            channel = rnd.choices(list(CHANNELS), list(CHANNELS.values()))[0]
            if customers and rnd.random() < 0.28:
                cust = rnd.choice(customers)
            else:
                cust, cid_next = cid_next, cid_next + 1
                customers.append(cust)
            items = rnd.sample(PRODUCTS, rnd.choice([1, 1, 1, 2]))
            total = round(sum(p for _, p in items), 2)
            created = day + timedelta(minutes=rnd.randint(0, 1439))
            o = {"id": oid, "created_at": ts(created), "updated_at": ts(created), "customer": {"id": cust, "email": f"c{cust}@customer.example"},
                 "total_price": f"{total:.2f}", "currency": "EUR", "financial_status": "paid",
                 "landing_site": f"/?utm_source={channel}", "line_items": [{"title": t, "price": f"{p:.2f}", "quantity": 1} for t, p in items]}
            ch = {"id": f"ch_{oid}", "object": "charge", "amount": round(total * 100), "currency": "eur", "created": int(created.timestamp()),
                  "metadata": {"order_id": str(oid)}, "amount_refunded": 0, "refunded": False}
            if rnd.random() < 0.06:  # some orders get refunded a few days later
                when = created + timedelta(days=rnd.randint(1, 6), minutes=rnd.randint(0, 600))
                amount = round(total * 100) if rnd.random() < 0.6 else round(items[0][1] * 100 * 0.5)
                refunds.append({"id": f"re_{oid}", "object": "refund", "charge": ch["id"], "amount": amount, "created": int(when.timestamp())})
                ch["amount_refunded"], ch["refunded"] = amount, amount == ch["amount"]
                o["financial_status"] = "refunded" if amount == ch["amount"] else "partially_refunded"
                o["updated_at"] = ts(when)
            orders.append(o)
            charges.append(ch)
            oid += 1
        for platform, share in (("google", 0.36), ("meta", 0.30)):
            for camp in ("brand", "prospecting"):
                spend = round(rnd.uniform(40, 120) * (1.4 if camp == "prospecting" else 0.6) * (1 + d / 80), 2)
                ads.append({"platform": platform, "date": (START + timedelta(days=d)).isoformat(), "campaign": f"{platform}-{camp}",
                            "spend": spend, "clicks": rnd.randint(80, 400), "impressions": rnd.randint(4000, 20000)})
    return {"orders": orders, "charges": charges, "refunds": refunds, "ads": ads}


def create_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)
    admin_token, api_token = os.environ["SOURCES_ADMIN_TOKEN"], os.environ["SOURCES_API_TOKEN"]
    data = generate()
    chaos = {"fail": 0, "renamed_column": False}

    def auth(token: str):
        if not secrets.compare_digest(token.removeprefix("Bearer ").strip(), api_token):
            raise HTTPException(401, "invalid token")
        if chaos["fail"] > 0:
            chaos["fail"] -= 1
            raise HTTPException(503, "temporarily unavailable")

    def admin(token: str):
        if not secrets.compare_digest(token, admin_token):
            raise HTTPException(403)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/shop/admin/api/2026-10/orders.json")
    def orders(request: Request, response: Response, updated_at_min: str = "", limit: int = 50, page_info: str = "",
               x_shopify_access_token: str = Header("")):
        auth(x_shopify_access_token)
        rows = sorted((o for o in data["orders"] if o["updated_at"] >= updated_at_min), key=lambda o: (o["updated_at"], o["id"]))
        start = int(page_info or 0)
        page = rows[start:start + min(limit, 250)]
        if start + len(page) < len(rows):
            response.headers["Link"] = f'<{request.url.include_query_params(page_info=start + len(page))}>; rel="next"'
        return {"orders": page}

    def stripe_list(kind: str, gte: int, limit: int, starting_after: str):
        rows = sorted((r for r in data[kind] if r["created"] >= gte), key=lambda r: (r["created"], r["id"]))
        if starting_after:
            idx = next((i for i, r in enumerate(rows) if r["id"] == starting_after), -1)
            rows = rows[idx + 1:]
        return {"object": "list", "data": rows[:min(limit, 100)], "has_more": len(rows) > limit}

    @app.get("/stripe/v1/charges")
    def charges(request: Request, limit: int = 100, starting_after: str = "", authorization: str = Header("")):
        auth(authorization)
        return stripe_list("charges", int(request.query_params.get("created[gte]", 0)), limit, starting_after)

    @app.get("/stripe/v1/refunds")
    def refunds(request: Request, limit: int = 100, starting_after: str = "", authorization: str = Header("")):
        auth(authorization)
        return stripe_list("refunds", int(request.query_params.get("created[gte]", 0)), limit, starting_after)

    @app.get("/ads/{platform}.csv")
    def ads(platform: str, since: str = "", authorization: str = Header("")):
        auth(authorization)
        if platform not in ("google", "meta"):
            raise HTTPException(404)
        # each platform exports its own column names, like the real ones
        cols = {"google": ["Day", "Campaign", "Cost", "Clicks", "Impr."], "meta": ["date_start", "campaign_name", "amount_spent", "clicks", "impressions"]}[platform]
        if chaos["renamed_column"] and platform == "google":
            cols = ["Day", "Campaign", "Cost (EUR)", "Clicks", "Impr."]  # what happens when someone changes the export
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(cols)
        for a in data["ads"]:
            if a["platform"] == platform and a["date"] >= since:
                w.writerow([a["date"], a["campaign"], f"{a['spend']:.2f}", a["clicks"], a["impressions"]])
        return Response(out.getvalue(), media_type="text/csv")

    @app.post("/_admin/chaos")
    def set_chaos(body: dict, x_admin: str = Header("")):
        admin(x_admin)
        for k in ("fail", "renamed_column"):
            if k in body:
                chaos[k] = body[k]
        if body.get("late_refund"):  # a refund today for an order from weeks ago: past days' net revenue must change
            o = next(o for o in data["orders"] if o["id"] == int(body["late_refund"]))
            ch = next(c for c in data["charges"] if c["metadata"]["order_id"] == str(o["id"]))
            latest = max([r["created"] for r in data["refunds"]] + [c["created"] for c in data["charges"]])
            now = datetime.fromtimestamp(latest + 3600, tz=timezone.utc)  # "now": after everything already in the data
            amount = ch["amount"] - ch["amount_refunded"]
            data["refunds"].append({"id": f"re_late_{o['id']}", "object": "refund", "charge": ch["id"], "amount": amount, "created": int(now.timestamp())})
            ch["amount_refunded"] += amount
            ch["refunded"], o["financial_status"], o["updated_at"] = True, "refunded", ts(now)
        if body.get("negative_spend"):
            data["ads"].append({"platform": "meta", "date": (START + timedelta(days=DAYS - 1)).isoformat(), "campaign": "meta-glitch",
                                "spend": -250.0, "clicks": 0, "impressions": 0})
        if body.get("clear_negative_spend"):
            data["ads"] = [a for a in data["ads"] if a["spend"] >= 0]
        return chaos

    @app.get("/_admin/truth")
    def truth(x_admin: str = Header("")):
        """Expected totals computed straight from the source data, independent of the pipeline's SQL."""
        admin(x_admin)
        gross = sum(float(o["total_price"]) for o in data["orders"])
        refunded = sum(r["amount"] for r in data["refunds"]) / 100
        spend = sum(a["spend"] for a in data["ads"] if a["spend"] >= 0)
        return {"orders": len(data["orders"]), "gross": round(gross, 2), "refunds": round(refunded, 2),
                "net": round(gross - refunded, 2), "ad_spend": round(spend, 2),
                "customers": len({o["customer"]["id"] for o in data["orders"]})}

    return app
