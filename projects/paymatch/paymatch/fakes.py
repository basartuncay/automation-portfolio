"""Demo stand-in for the accounting system (sevDesk/lexoffice style): paginated open invoices and a payments endpoint
that is idempotent by external_id. /_admin endpoints (token-protected) read state and inject outages."""
import os, secrets

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from .demo_data import INVOICES


def create_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)
    token, admin_token = os.environ["ACCOUNTING_TOKEN"], os.environ["FAKES_ADMIN_TOKEN"]
    inv = {i[0]: {"id": i[0], "number": i[1], "customer": i[2], "customer_no": i[3], "email": i[4], "issued": i[5], "due": i[6],
                  "total_cents": i[7], "open_cents": i[7], "payments": []} for i in INVOICES}
    seen, chaos = {}, {"fail": 0}

    def auth(h: str):
        if not secrets.compare_digest(h.removeprefix("Bearer "), token):
            raise HTTPException(401)
        if chaos["fail"] > 0:
            chaos["fail"] -= 1
            raise HTTPException(503, "maintenance")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/accounting/v1/invoices")
    def invoices(status: str = "open", page: int = 1, authorization: str = Header("")):
        auth(authorization)
        rows = [{k: v for k, v in i.items() if k != "payments"} for i in inv.values() if status != "open" or i["open_cents"] > 0]
        size = 5
        return {"data": rows[(page - 1) * size: page * size], "next_page": page + 1 if page * size < len(rows) else None}

    @app.post("/accounting/v1/invoices/{iid}/payments")
    def pay(iid: str, body: dict, authorization: str = Header("")):
        auth(authorization)
        if body["external_id"] in seen:
            return JSONResponse({"error": "duplicate external_id", "payment": seen[body["external_id"]]}, 409)
        i = inv.get(iid)
        if not i:
            raise HTTPException(404)
        total = body["amount_cents"] + (body.get("writeoff_cents") or 0)
        if total > i["open_cents"]:
            return JSONResponse({"error": f"payment of {total} exceeds the open amount {i['open_cents']}"}, 422)
        i["open_cents"] -= total
        p = {**body, "invoice": iid}
        i["payments"].append(p)
        seen[body["external_id"]] = p
        return JSONResponse(p, 201)

    @app.post("/_admin/chaos")
    def set_chaos(body: dict, x_admin: str = Header("")):
        if not secrets.compare_digest(x_admin, admin_token):
            raise HTTPException(403)
        chaos["fail"] = int(body.get("fail", 0))
        return chaos

    @app.post("/_admin/pay_elsewhere")
    def elsewhere(body: dict, x_admin: str = Header("")):
        """Someone books a payment directly in the accounting system (e.g. cash)."""
        if not secrets.compare_digest(x_admin, admin_token):
            raise HTTPException(403)
        inv[body["id"]]["open_cents"] = 0
        return inv[body["id"]]

    @app.get("/_admin/state")
    def state(x_admin: str = Header("")):
        if not secrets.compare_digest(x_admin, admin_token):
            raise HTTPException(403)
        return inv

    return app
