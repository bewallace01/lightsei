"""Phase 17.4: Stripe billing helper.

Pure module wrapping the Stripe SDK so the request handlers in main.py
stay thin and tests can stub stripe.* calls at module-load time.
Mirrors the shape of google_oauth.py.

Configured via env:
  - LIGHTSEI_STRIPE_SECRET_KEY: sk_test_... in dev, sk_live_... in prod.
  - LIGHTSEI_STRIPE_PRICE_ID: the recurring $50/mo price configured in
    the Stripe dashboard. The Checkout session subscribes the customer
    to this price.
  - LIGHTSEI_STRIPE_WEBHOOK_SECRET: whsec_... from the webhook endpoint
    you registered in the Stripe dashboard. Used to verify webhook
    delivery signatures so attackers can't forge subscription events.
  - LIGHTSEI_DASHBOARD_BASE_URL: where Checkout + the Customer Portal
    redirect the user back to (defaults to https://app.lightsei.com).

See STRIPE_SETUP.md in the repo root for the dashboard-side configuration.

Tests stub `stripe.Customer.create`, `stripe.checkout.Session.create`,
`stripe.billing_portal.Session.create`, and `stripe.Webhook.construct_event`
so they don't hit Stripe; production needs the dashboard configured before
the endpoints in main.py can complete.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

import stripe

logger = logging.getLogger("lightsei.stripe_billing")


def _secret_key() -> Optional[str]:
    return os.environ.get("LIGHTSEI_STRIPE_SECRET_KEY")


def _price_id() -> Optional[str]:
    return os.environ.get("LIGHTSEI_STRIPE_PRICE_ID")


def _webhook_secret() -> Optional[str]:
    return os.environ.get("LIGHTSEI_STRIPE_WEBHOOK_SECRET")


def _dashboard_base_url() -> str:
    return os.environ.get(
        "LIGHTSEI_DASHBOARD_BASE_URL", "https://app.lightsei.com"
    ).rstrip("/")


def is_configured() -> bool:
    """True when the Stripe secret key + price ID are wired. The billing
    endpoints in main.py 503 if not, so misconfigured deployments fail
    loud rather than serving half-broken Checkout sessions."""
    return bool(_secret_key() and _price_id())


def is_webhook_configured() -> bool:
    """Separate check because the webhook secret comes from a different
    place in the Stripe dashboard (create the endpoint first, copy the
    signing secret second) and is easy to forget in initial setup."""
    return bool(_webhook_secret())


def _ensure_api_key() -> None:
    """Stripe's SDK is module-global — set the api_key on every call so
    a hot-reload of the env (e.g. when tests rotate keys) is picked up
    without re-importing the module."""
    key = _secret_key()
    if not key:
        raise StripeNotConfiguredError(
            "LIGHTSEI_STRIPE_SECRET_KEY is not set"
        )
    stripe.api_key = key


class StripeNotConfiguredError(Exception):
    """Raised when one of the required env vars is missing. The handler
    in main.py converts to a 503."""


class StripeApiError(Exception):
    """Wraps any stripe.* call failure. The handler in main.py converts
    to a 502 so the user sees "billing is temporarily unavailable" rather
    than a stack trace."""


def create_customer(*, email: str, workspace_id: str) -> str:
    """Create a Stripe Customer for `workspace_id`. Returns the
    `cus_...` id. Called lazily from the checkout endpoint the first
    time a workspace tries to upgrade (most workspaces never will, so
    eager creation on every signup would waste Stripe API calls).

    The `metadata.workspace_id` tag is what we use in the webhook
    handler to look the workspace back up when Stripe pushes us a
    subscription event (Stripe's event payloads carry the customer id
    but we'd rather index our own workspace id directly).
    """
    _ensure_api_key()
    try:
        customer = stripe.Customer.create(
            email=email,
            metadata={"workspace_id": workspace_id, "source": "lightsei"},
            description=f"Lightsei workspace {workspace_id}",
        )
    except stripe.StripeError as exc:
        logger.exception("stripe: customer create failed")
        raise StripeApiError(f"customer create failed: {exc}") from exc
    return customer["id"]


def create_checkout_session(
    *,
    customer_id: str,
    workspace_id: str,
    success_path: str = "/account?upgrade=success",
    cancel_path: str = "/account?upgrade=cancelled",
) -> dict[str, Any]:
    """Create a Stripe Checkout session in subscription mode for the
    configured $50/mo price. Returns `{url, id}`.

    success_path / cancel_path are appended to LIGHTSEI_DASHBOARD_BASE_URL
    so the user lands back on /account with a flag the dashboard can
    use to poll for plan_tier='paid' (the webhook lands within seconds).
    """
    _ensure_api_key()
    price_id = _price_id()
    if not price_id:
        raise StripeNotConfiguredError(
            "LIGHTSEI_STRIPE_PRICE_ID is not set"
        )

    base = _dashboard_base_url()
    success_url = f"{base}{success_path}"
    cancel_url = f"{base}{cancel_path}"

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            customer=customer_id,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            client_reference_id=workspace_id,
            metadata={"workspace_id": workspace_id},
            subscription_data={
                # Mirror the workspace_id onto the subscription too so
                # downstream invoice / subscription events carry it
                # without needing to re-fetch the parent customer.
                "metadata": {"workspace_id": workspace_id},
            },
        )
    except stripe.StripeError as exc:
        logger.exception("stripe: checkout session create failed")
        raise StripeApiError(f"checkout session create failed: {exc}") from exc

    return {"url": session["url"], "id": session["id"]}


def create_portal_session(
    *, customer_id: str, return_path: str = "/account"
) -> dict[str, Any]:
    """Create a Stripe Customer Portal session so the user can manage
    payment methods, cancel, view invoices. Returns `{url, id}`.

    The Portal config (which features are enabled) lives in the Stripe
    dashboard — STRIPE_SETUP.md covers it. With the default config users
    can update card / cancel subscription / see invoice history.
    """
    _ensure_api_key()
    base = _dashboard_base_url()
    return_url = f"{base}{return_path}"

    try:
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=return_url,
        )
    except stripe.StripeError as exc:
        logger.exception("stripe: portal session create failed")
        raise StripeApiError(f"portal session create failed: {exc}") from exc

    return {"url": session["url"], "id": session["id"]}


class WebhookSignatureError(Exception):
    """Raised when the webhook signature header doesn't verify against
    the configured signing secret. The handler returns 400; never 5xx,
    since 5xx makes Stripe retry forever."""


def construct_webhook_event(
    *, payload: bytes, signature_header: str
) -> dict[str, Any]:
    """Verify the Stripe-Signature header and parse the JSON body.
    Returns the parsed event dict (Stripe SDK returns a dict-like
    `stripe.Event` object).

    Raises WebhookSignatureError on bad signature / missing secret —
    the handler converts both to 400 so Stripe stops retrying a
    permanently-broken delivery.
    """
    secret = _webhook_secret()
    if not secret:
        raise WebhookSignatureError(
            "LIGHTSEI_STRIPE_WEBHOOK_SECRET is not set"
        )
    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=signature_header,
            secret=secret,
        )
    except (ValueError, stripe.SignatureVerificationError) as exc:
        logger.warning("stripe webhook: signature verification failed: %s", exc)
        raise WebhookSignatureError(str(exc)) from exc

    # stripe.Event is dict-like; convert for callers that expect a plain
    # dict (e.g. tests asserting on shape).
    if hasattr(event, "to_dict"):
        return event.to_dict()
    return dict(event)
