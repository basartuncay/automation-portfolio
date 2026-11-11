"""Demo stand-ins for the three paid APIs a lead pipeline uses, with their real request shapes, so nothing touches a
real account and every run is repeatable:

- /enrich  company enrichment by domain (Clearbit/Apollo style): industry, size, country, tech stack; 404 if unknown
- /verify  email verification (ZeroBounce style): valid, invalid, catch-all, unknown, do_not_mail
- /crm     HubSpot-style contacts and deals, 409 on a duplicate email
/_admin endpoints (token-protected) inject rate limits and outages and read the CRM.
"""
import os, secrets, time

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

COMPANIES = {  # domain: name, industry, employees, country, tech, accepts any address
    "nordlicht-shop.de": ("Nordlicht Shop", "E-commerce", 34, "DE", ["Shopify", "Klaviyo"], False),
    "kaffeekontor.de": ("Kaffeekontor Hamburg", "Consumer Goods", 58, "DE", ["Shopware"], False),
    "bergwerk-outdoor.at": ("Bergwerk Outdoor", "Retail", 120, "AT", ["WooCommerce"], False),
    "velo-atelier.ch": ("Velo Atelier", "Retail", 22, "CH", ["Shopify"], True),
    "tulpen-design.nl": ("Tulpen Design", "E-commerce", 15, "NL", ["Shopify"], False),
    "fabrikat-moebel.de": ("Fabrikat Möbel", "Furniture", 240, "DE", ["Magento"], False),
    "greenleaf-cosmetics.de": ("Greenleaf Cosmetics", "Consumer Goods", 75, "DE", ["Shopify", "Recharge"], False),
    "datawerk-consulting.de": ("Datawerk Consulting", "IT Services", 12, "DE", ["WordPress"], False),
    "mega-retail-group.com": ("Mega Retail Group", "Retail", 12000, "GB", ["Salesforce Commerce"], False),
    "pfoten-glueck.de": ("Pfotenglück", "E-commerce", 9, "DE", ["Shopify"], False),
    "brauhaus-direkt.de": ("Brauhaus Direkt", "Consumer Goods", 41, "DE", ["WooCommerce"], True),
    "lumen-lighting.nl": ("Lumen Lighting", "E-commerce", 63, "NL", ["Shopware"], False),
    "steuerkanzlei-maier.de": ("Steuerkanzlei Maier", "Accounting", 18, "DE", [], False),
    "alpaka-wolle.at": ("Alpaka Wolle", "E-commerce", 11, "AT", ["Shopify"], False),
    "urban-sneaker.de": ("Urban Sneaker", "E-commerce", 88, "DE", ["Shopify Plus"], False),
}
INVALID = {"m.schulz@kaffeekontor.de", "old.contact@fabrikat-moebel.de"}  # mailboxes that no longer exist


def create_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)
    api_key, admin_token = os.environ["DATA_API_KEY"], os.environ["FAKES_ADMIN_TOKEN"]
    crm_token = os.environ["CRM_TOKEN"]
    chaos = {"rate_limit": 0, "fail": 0}
    stats = {"enrich_calls": 0, "verify_calls": 0}
    crm = {"contacts": [], "deals": []}

    def check(header: str, expected: str):
        if not secrets.compare_digest(header.removeprefix("Bearer ").strip(), expected):
            raise HTTPException(401, "invalid api key")
        if chaos["rate_limit"] > 0:
            chaos["rate_limit"] -= 1
            raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": "1"})
        if chaos["fail"] > 0:
            chaos["fail"] -= 1
            raise HTTPException(503, "temporarily unavailable")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/enrich/v1/companies/find")
    def enrich(domain: str, authorization: str = Header("")):
        check(authorization, api_key)
        stats["enrich_calls"] += 1
        c = COMPANIES.get(domain.lower())
        if not c:
            raise HTTPException(404, "company not found")
        name, industry, employees, country, tech, _ = c
        return {"domain": domain, "name": name, "industry": industry, "metrics": {"employees": employees},
                "geo": {"countryCode": country}, "tech": tech}

    @app.get("/verify/v2/validate")
    def verify(email: str, x_api_key: str = Header("")):
        check(x_api_key, api_key)
        stats["verify_calls"] += 1
        local, _, domain = email.lower().partition("@")
        if email.lower() in INVALID or local in ("noreply", "no-reply", "bounce"):
            return {"address": email, "status": "invalid", "sub_status": "mailbox_not_found"}
        if local in ("abuse", "postmaster", "spam"):
            return {"address": email, "status": "do_not_mail", "sub_status": "role_based"}
        c = COMPANIES.get(domain)
        if c and c[5]:
            return {"address": email, "status": "catch-all", "sub_status": ""}
        if c or domain in ("gmail.com", "gmx.de", "web.de"):
            return {"address": email, "status": "valid", "sub_status": ""}
        return {"address": email, "status": "unknown", "sub_status": "mail_server_did_not_respond"}

    @app.post("/crm/v3/objects/contacts")
    def contact(body: dict, authorization: str = Header("")):
        check(authorization, crm_token)
        p = body.get("properties", {})
        old = next((c for c in crm["contacts"] if c["properties"]["email"] == p.get("email")), None)
        if old:
            return JSONResponse({"status": "error", "category": "CONFLICT", "message": f"Contact already exists. Existing ID: {old['id']}"}, 409)
        c = {"id": str(5000 + len(crm["contacts"])), "properties": p, "createdAt": time.time()}
        crm["contacts"].append(c)
        return JSONResponse(c, 201)

    @app.post("/crm/v3/objects/deals")
    def deal(body: dict, authorization: str = Header("")):
        check(authorization, crm_token)
        d = {"id": str(9000 + len(crm["deals"])), "properties": body.get("properties", {}), "associations": body.get("associations", [])}
        crm["deals"].append(d)
        return JSONResponse(d, 201)

    def admin(x_admin: str):
        if not secrets.compare_digest(x_admin, admin_token):
            raise HTTPException(403)

    @app.post("/_admin/chaos")
    def set_chaos(body: dict, x_admin: str = Header("")):
        admin(x_admin)
        chaos.update({k: int(v) for k, v in body.items() if k in chaos})
        return chaos

    @app.get("/_admin/state")
    def state(x_admin: str = Header("")):
        admin(x_admin)
        return {"crm": crm, "stats": stats}

    return app
