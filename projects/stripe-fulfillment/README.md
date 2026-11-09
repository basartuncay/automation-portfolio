# Stripe Webhooks → Automatic Fulfillment

When a customer pays through Stripe Checkout, this service issues a license key and emails it. It also keeps access in sync with the subscription: past-due on a failed payment, reactivated when the invoice is paid, revoked on cancellation. A `GET /licenses/{key}` endpoint lets the app check a key.

## Why it's safe

- **Every webhook is verified** with Stripe's SDK (`construct_event`): HMAC signature plus a 5-minute timestamp window. Forged or replayed requests get a 400.
- **Fulfils exactly once.** Stripe retries deliveries and can send an event twice. The event id is claimed inside the same database transaction as the fulfilment, so even two simultaneous deliveries can't issue two licenses.
- **All or nothing.** If anything fails mid-way (say the mail server is down), the transaction rolls back and the endpoint returns 500, so Stripe retries later. A retry never finds a half-done order.
- **Handles async payments:** bank debits that settle later aren't fulfilled until `checkout.session.async_payment_succeeded` arrives.
- **No duplicate active licenses** per customer and product.

| Event | Action |
|---|---|
| `checkout.session.completed` / `async_payment_succeeded` | create license, email the key |
| `invoice.payment_failed` | license → `past_due`, email the payment link |
| `invoice.paid` | `past_due` → `active` |
| `customer.subscription.deleted` | license → `revoked` |
| anything else | acknowledged, ignored |

## Run it

```bash
uv sync
export STRIPE_WEBHOOK_SECRET=whsec_...     # from `stripe listen` or the Dashboard webhook endpoint
uv run uvicorn stripe_fulfillment.app:app_from_env --factory --port 8000
stripe listen --forward-to localhost:8000/webhooks/stripe   # local testing with the Stripe CLI
stripe trigger checkout.session.completed
```

Put the product name in Checkout Session `metadata.product`. Emails are logged by default. Swap `ConsoleMailer` for SMTP, Postmark or Resend; it is a single `send(to, subject, body)` method.

## Tests

```bash
uv run pytest -q
```

They cover:
- a paid checkout producing a license and an email
- a wrong signature and a 1-hour-old timestamp, both rejected
- a retried event fulfilled once
- a second event not creating a second active license
- an unpaid async checkout waiting
- the full lifecycle: failed payment → paid → cancelled
- rollback on failure, then a successful retry
- unknown events ignored and unknown license keys returning 404
- key uniqueness

The tests sign payloads exactly as Stripe does and run offline.
