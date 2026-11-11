"""Clients for the sales channels and the warehouse, with each API's real request shape:

- Shopify Admin REST: inventory_levels/set.json and inventory_levels.json, X-Shopify-Access-Token
- Amazon SP-API: Listings Items PATCH (fulfillment_availability), Orders getOrders/getOrderItems, x-amz-access-token
- Warehouse (WMS): a stock snapshot endpoint; shipments arrive by signed webhook (see web.py)
429 means slow down (Retry-After), 5xx and network errors are temporary, other 4xx are permanent for that request.
"""
import os

import httpx


class Throttled(Exception):
    def __init__(self, wait: float):
        super().__init__(f"throttled, retry in {wait:g}s")
        self.wait = wait


class Temporary(Exception):
    pass


def check(r: httpx.Response) -> httpx.Response:
    if r.status_code == 429:
        raise Throttled(float(r.headers.get("retry-after", 2)))
    if r.status_code >= 500:
        raise Temporary(f"{r.status_code} from {r.request.url.host}")
    r.raise_for_status()
    return r


class Shopify:
    name = "shopify"

    def __init__(self, http: httpx.Client | None = None):
        self.base = os.environ["SHOPIFY_URL"].rstrip("/") + "/admin/api/2026-10"
        self.location = os.environ.get("SHOPIFY_LOCATION_ID", "loc_1")
        self.http = http or httpx.Client(timeout=15)
        self.http.headers["X-Shopify-Access-Token"] = os.environ["SHOPIFY_TOKEN"]

    def set_quantity(self, ref: str, qty: int) -> None:
        check(self.http.post(f"{self.base}/inventory_levels/set.json", json={"location_id": self.location, "inventory_item_id": ref, "available": qty}))

    def quantities(self, refs: list[str]) -> dict[str, int]:
        r = check(self.http.get(f"{self.base}/inventory_levels.json", params={"inventory_item_ids": ",".join(refs), "location_ids": self.location}))
        return {l["inventory_item_id"]: l["available"] for l in r.json()["inventory_levels"]}


class Amazon:
    name = "amazon"

    def __init__(self, http: httpx.Client | None = None):
        self.base = os.environ["AMAZON_URL"].rstrip("/")
        self.seller = os.environ.get("AMAZON_SELLER_ID", "A1SELLER")
        self.marketplace = os.environ.get("AMAZON_MARKETPLACE_ID", "A1PA6795UKMFR9")  # amazon.de
        self.http = http or httpx.Client(timeout=15)
        self.http.headers["x-amz-access-token"] = os.environ["AMAZON_TOKEN"]

    def set_quantity(self, ref: str, qty: int) -> None:
        body = {"productType": "PRODUCT", "patches": [{"op": "replace", "path": "/attributes/fulfillment_availability",
                                                       "value": [{"fulfillment_channel_code": "DEFAULT", "quantity": qty}]}]}
        r = check(self.http.patch(f"{self.base}/listings/2021-08-01/items/{self.seller}/{ref}", params={"marketplaceIds": self.marketplace}, json=body))
        if r.json().get("status") != "ACCEPTED":
            raise ValueError(f"listing update not accepted: {r.json().get('issues')}")

    def quantities(self, refs: list[str]) -> dict[str, int]:
        out = {}
        for ref in refs:
            r = check(self.http.get(f"{self.base}/listings/2021-08-01/items/{self.seller}/{ref}",
                                    params={"marketplaceIds": self.marketplace, "includedData": "fulfillmentAvailability"}))
            out[ref] = r.json()["fulfillmentAvailability"][0]["quantity"]
        return out

    def orders_since(self, since: str) -> list[dict]:
        """Orders changed since `since` (ISO 8601), with their items, following NextToken pages."""
        out, token = [], None
        while True:
            params = {"MarketplaceIds": self.marketplace, "LastUpdatedAfter": since} | ({"NextToken": token} if token else {})
            page = check(self.http.get(f"{self.base}/orders/v0/orders", params=params)).json()["payload"]
            for o in page["Orders"]:
                items = check(self.http.get(f"{self.base}/orders/v0/orders/{o['AmazonOrderId']}/orderItems")).json()["payload"]["OrderItems"]
                out.append({**o, "items": items})
            token = page.get("NextToken")
            if not token:
                return out


class Warehouse:
    def __init__(self, http: httpx.Client | None = None):
        self.base = os.environ["WMS_URL"].rstrip("/")
        self.http = http or httpx.Client(timeout=15)
        self.http.headers["Authorization"] = "Bearer " + os.environ["WMS_TOKEN"]

    def snapshot(self) -> dict[str, int]:
        return {r["sku"]: r["on_hand"] for r in check(self.http.get(f"{self.base}/v1/stock")).json()["items"]}
