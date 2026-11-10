"""Stripe webhooks -> fulfillment. Each event is verified, processed exactly once, and only marked done on success."""
import logging, os, secrets, sqlite3
from contextlib import closing
from typing import Protocol

import stripe
from fastapi import FastAPI, Header, HTTPException, Request

log = logging.getLogger("fulfillment")

SCHEMA = """
create table if not exists events (id text primary key, type text, processed_at text default current_timestamp);
create table if not exists licenses (
  key text primary key, customer text, email text, product text, status text, created_at text default current_timestamp);
create index if not exists licenses_customer on licenses(customer);
"""


class Mailer(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...


class ConsoleMailer:
    def send(self, to, subject, body):
        log.info("EMAIL to=%s subject=%s\n%s", to, subject, body)


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.executescript(SCHEMA)

    def processed(self, event_id: str) -> bool:
        return self.db.execute("select 1 from events where id=?", (event_id,)).fetchone() is not None


def license_key() -> str:
    return "LIC-" + "-".join(secrets.token_hex(2).upper() for _ in range(4))


def handle(event: dict, db: sqlite3.Connection, mailer: Mailer) -> str:
    """Apply one event inside the caller's transaction. Returns what was done (for logs and tests)."""
    t, obj = event["type"], event["data"]["object"]

    if t in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        if obj.get("payment_status") not in ("paid", "no_payment_required"):
            return "waiting for async payment"  # bank debits etc. complete later via async_payment_succeeded
        customer = obj.get("customer") or obj["id"]
        email = (obj.get("customer_details") or {}).get("email")
        product = (obj.get("metadata") or {}).get("product", "default")
        existing = db.execute("select key from licenses where customer=? and product=? and status='active'",
                              (customer, product)).fetchone()
        if existing:
            return f"already active {existing[0]}"
        key = license_key()
        db.execute("insert into licenses(key, customer, email, product, status) values (?,?,?,?, 'active')",
                   (key, customer, email, product))
        if email:
            mailer.send(email, "Your license key", f"Thanks for your purchase!\n\nLicense key: {key}\nProduct: {product}")
        return f"granted {key}"

    if t == "customer.subscription.deleted":
        n = db.execute("update licenses set status='revoked' where customer=? and status!='revoked'",
                       (obj["customer"],)).rowcount
        return f"revoked {n}"

    if t == "invoice.payment_failed":
        db.execute("update licenses set status='past_due' where customer=? and status='active'", (obj["customer"],))
        if obj.get("customer_email"):
            mailer.send(obj["customer_email"], "Payment failed",
                        f"We couldn't charge your card. Please update it here: {obj.get('hosted_invoice_url', '')}")
        return "marked past_due"

    if t == "invoice.paid":
        n = db.execute("update licenses set status='active' where customer=? and status='past_due'",
                       (obj["customer"],)).rowcount
        return f"reactivated {n}"

    return "ignored"


def create_app(secret: str, store: Store, mailer: Mailer) -> FastAPI:
    app = FastAPI(title="Stripe fulfillment")

    @app.post("/webhooks/stripe")
    async def webhook(request: Request, stripe_signature: str | None = Header(None)):
        payload = await request.body()
        try:
            event = stripe.Webhook.construct_event(payload, stripe_signature, secret)  # checks HMAC and 5-min timestamp window
        except (ValueError, stripe.SignatureVerificationError):
            raise HTTPException(400, "invalid signature")
        event = event.to_dict()
        with closing(store.db.cursor()) as cur:
            cur.execute("begin immediate")
            try:
                # Claim the event id first, inside the transaction: two concurrent deliveries can't both fulfil.
                cur.execute("insert into events(id, type) values (?,?)", (event["id"], event["type"]))
            except sqlite3.IntegrityError:
                cur.execute("rollback")
                return {"duplicate": True}  # Stripe retries and may deliver twice: never fulfil twice
            try:
                result = handle(event, store.db, mailer)
                cur.execute("commit")
            except Exception:
                cur.execute("rollback")
                log.exception("event %s failed", event["id"])
                raise HTTPException(500, "processing failed")  # non-2xx makes Stripe retry later
        log.info("%s %s -> %s", event["id"], event["type"], result)
        return {"ok": True, "result": result}

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/licenses/{key}")
    def check(key: str):
        row = store.db.execute("select status, product from licenses where key=?", (key,)).fetchone()
        if not row:
            raise HTTPException(404, "unknown license")
        return {"valid": row[0] == "active", "status": row[0], "product": row[1]}

    return app


def app_from_env() -> FastAPI:
    logging.basicConfig(level=logging.INFO)
    return create_app(os.environ["STRIPE_WEBHOOK_SECRET"], Store(os.environ.get("DB_PATH", "fulfillment.sqlite")),
                      ConsoleMailer())
