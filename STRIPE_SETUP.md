# Stripe setup for Lightsei (Phase 17.4)

The backend code for Stripe billing is wired and tested (see
`backend/stripe_billing.py`, `backend/tests/test_stripe_billing.py`).
This doc covers the Stripe dashboard configuration you need to do
once, in test mode first and again in live mode when you're ready.

The endpoints stay 503 until the env vars below are set, so you can
deploy the code first and configure Stripe later without breaking
anything.

## TL;DR

You need to collect four values from the Stripe dashboard and set them
as env vars on every backend service that talks to Stripe (Railway
backend service, local dev, and the worker if it ever needs to read
plan_tier):

| Env var | Where it comes from |
| --- | --- |
| `LIGHTSEI_STRIPE_SECRET_KEY` | Stripe dashboard → Developers → API keys |
| `LIGHTSEI_STRIPE_PRICE_ID` | Stripe dashboard → Products → (the $50/mo product) → Pricing |
| `LIGHTSEI_STRIPE_WEBHOOK_SECRET` | Stripe dashboard → Developers → Webhooks → (your endpoint) → Signing secret |
| `LIGHTSEI_DASHBOARD_BASE_URL` | Wherever the dashboard is reachable, e.g. `https://app.lightsei.com` |

Do the test-mode setup first. Test-mode values use `sk_test_...`,
`price_...`, and `whsec_...` prefixes; live-mode values use `sk_live_...`
(`price_...` and `whsec_...` are the same prefix in both modes but
they're different objects).

## Step 1: create the product + price

1. Stripe dashboard → Products → Add product.
2. Name: `Lightsei`. Description: `Configure-your-team AI agent
   platform.` Image optional.
3. Pricing: $50.00 USD, recurring, monthly. Currency USD.
4. Save. Copy the `price_...` id from the product detail page.

This is `LIGHTSEI_STRIPE_PRICE_ID`. The Checkout session in
`stripe_billing.create_checkout_session` subscribes the customer to
this price.

## Step 2: grab the API key

1. Stripe dashboard → Developers → API keys.
2. Reveal the Secret key (`sk_test_...` in test mode).
3. Copy it. This is `LIGHTSEI_STRIPE_SECRET_KEY`.

The publishable key (`pk_...`) is NOT needed — Lightsei does all
Stripe calls server-side; the dashboard never talks to Stripe
directly (it just navigates the browser to the Checkout URL we hand
it).

## Step 3: configure the Customer Portal

The portal is what `/workspaces/me/billing/portal` opens for users
who want to update their card, view invoices, or cancel.

1. Stripe dashboard → Settings → Billing → Customer portal.
2. Enable: "Customers can update payment methods," "Customers can view
   invoice history," "Customers can cancel subscriptions."
3. Cancellation: pick "Cancel at end of billing period" (so users
   keep paid access through what they've already paid for). When the
   period ends, Stripe sends a `customer.subscription.deleted` event
   and the webhook handler downgrades them to free.
4. Business information: fill in your business name + support email
   so the portal page looks legitimate.
5. Save.

No env var for this — the portal config is bound to the account, not
the API call.

## Step 4: create the webhook endpoint

1. Stripe dashboard → Developers → Webhooks → Add endpoint.
2. Endpoint URL: `https://api.lightsei.com/billing/stripe/webhook`
   (or whatever your prod backend host is; for local testing use the
   `stripe listen` CLI command — see "Local testing" below).
3. Events to send: select these five (the handler in `main.py`
   ignores everything else with a 200 ack):
   - `checkout.session.completed`
   - `customer.subscription.created`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `invoice.payment_failed`
4. Create endpoint.
5. On the endpoint detail page, reveal the Signing secret
   (`whsec_...`) and copy it. This is `LIGHTSEI_STRIPE_WEBHOOK_SECRET`.

Without this secret the webhook endpoint returns 400 (intentionally,
so a misconfigured deployment doesn't accept forged events).

## Step 5: set the env vars on Railway

```
railway variables set LIGHTSEI_STRIPE_SECRET_KEY=sk_test_...
railway variables set LIGHTSEI_STRIPE_PRICE_ID=price_...
railway variables set LIGHTSEI_STRIPE_WEBHOOK_SECRET=whsec_...
railway variables set LIGHTSEI_DASHBOARD_BASE_URL=https://app.lightsei.com
```

Run on the `lightsei-backend` service. The dashboard service doesn't
need them (it just calls the backend's billing endpoints).

Redeploy the backend (`railway up`) to pick them up. After redeploy:

```
curl -s https://api.lightsei.com/billing/stripe/webhook \
  -X POST -d '{}' \
  -H 'stripe-signature: stub'
# → 400 "bad signature: ..."  (means the secret is set + verifying)
```

If you get 400 "webhook secret not configured" instead, the env var
didn't make it onto the service.

## Step 6: end-to-end smoke test

1. In the dashboard, sign in as a fresh user (gets `plan_tier=free`
   and $5 of credits).
2. Visit `/account`. Click "upgrade to $50/mo" (this UI is 17.7 — if
   it's not built yet, hit the endpoint directly with curl:
   `POST /workspaces/me/billing/checkout` and open the returned
   `checkout_url` in a browser).
3. Use Stripe's test card `4242 4242 4242 4242`, any future date,
   any CVC.
4. After paying, Stripe redirects back to
   `/account?upgrade=success`. The webhook delivers
   `checkout.session.completed` to your backend within ~1 second; the
   workspace row flips to `plan_tier=paid`. Reload `/account` and
   the plan badge should say Paid.
5. Click "manage subscription." Stripe Customer Portal opens. Cancel
   the subscription. Stripe sends `customer.subscription.deleted`;
   the workspace flips back to `plan_tier=free`.

If the smoke test passes in test mode, repeat steps 1-5 in live mode
with real `sk_live_...` values and a real card.

## Local testing with stripe CLI

For local development without exposing your laptop to the public
internet:

```
# One-time install of the Stripe CLI:
brew install stripe/stripe-cli/stripe
stripe login

# Forward webhook deliveries to your local backend:
stripe listen --forward-to localhost:8000/billing/stripe/webhook
# → prints a `whsec_...` signing secret unique to this listen session.
# Use THAT as LIGHTSEI_STRIPE_WEBHOOK_SECRET for the local backend.

# Trigger a fake event to test:
stripe trigger checkout.session.completed
```

The local listen secret is different from the dashboard webhook
endpoint's secret. Don't mix them up.

## What the four backend endpoints do

| Endpoint | Auth | Behavior |
| --- | --- | --- |
| `POST /workspaces/me/billing/checkout` | session | Creates Stripe customer (lazy) + Checkout session, returns `{checkout_url}`. Dashboard navigates the browser there. |
| `POST /workspaces/me/billing/portal` | session | Returns Customer Portal URL for managing card / cancelling / invoices. 400 if user has never been to Checkout. |
| `POST /billing/stripe/webhook` | Stripe signature | Verifies `stripe-signature` header, then flips `plan_tier` based on event type. Idempotent on duplicate delivery. |

The paywall middleware (`backend/billing_gate.py`, Phase 17.5)
already fires 402 when `plan_tier='free'` and credits are exhausted,
so once a workspace flips to `paid` via the webhook the paywall stops
firing for them.

## Failure surfaces

- **503 from `/billing/checkout` or `/billing/portal`** → one of the
  three env vars is missing. Check Railway variables, redeploy.
- **502 from `/billing/checkout`** → Stripe API call failed (network,
  invalid price, account in restricted state). Check Stripe dashboard
  → Developers → Logs for the actual error.
- **400 from `/billing/stripe/webhook` with "bad signature"** →
  signing secret mismatch (e.g. you copied the test-mode secret to
  prod, or the endpoint was deleted + recreated in Stripe).
- **400 from webhook with "webhook secret not configured"** →
  `LIGHTSEI_STRIPE_WEBHOOK_SECRET` is unset on the service that
  received the request.
