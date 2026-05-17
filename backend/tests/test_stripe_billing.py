"""Phase 17.4: Stripe billing tests.

Three surfaces:

1. `stripe_billing` helper module — env-var detection + thin SDK wrapper.
2. `/workspaces/me/billing/checkout` + `/workspaces/me/billing/portal`
   endpoints — lazy customer creation, paid-already short-circuit,
   503/502 surfaces when Stripe is misconfigured / errors.
3. `/billing/stripe/webhook` — signature verification, subscription
   lifecycle event handling (plan_tier flip), idempotency on duplicate
   delivery.

Stripe SDK calls are stubbed via monkeypatch — we don't hit Stripe and
we don't bother computing real signature headers (that's the SDK's job;
we test our usage of it).
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

import stripe_billing
from db import session_scope
from models import Workspace
from tests.conftest import auth_headers


# ---------- helpers ---------- #


@pytest.fixture(autouse=True)
def _stripe_env(monkeypatch):
    """Pre-configure the three required env vars so is_configured()
    returns True for every test. Individual tests that need the
    not-configured path will clear them explicitly."""
    monkeypatch.setenv("LIGHTSEI_STRIPE_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setenv("LIGHTSEI_STRIPE_PRICE_ID", "price_test_dummy")
    monkeypatch.setenv("LIGHTSEI_STRIPE_WEBHOOK_SECRET", "whsec_test_dummy")
    monkeypatch.setenv(
        "LIGHTSEI_DASHBOARD_BASE_URL", "https://dashboard.test"
    )


def _stub_stripe_calls(monkeypatch, *, customer_id="cus_test_123"):
    """Replace the three stripe-call wrappers in stripe_billing so they
    don't hit Stripe. Returns the call-log dict so tests can assert on
    what was sent."""
    calls: dict = {"create_customer": [], "checkout": [], "portal": []}

    def fake_create_customer(*, email, workspace_id):
        calls["create_customer"].append({"email": email, "workspace_id": workspace_id})
        return customer_id

    def fake_create_checkout_session(*, customer_id, workspace_id, **kwargs):
        calls["checkout"].append({"customer_id": customer_id, "workspace_id": workspace_id})
        return {"url": "https://checkout.stripe.com/c/pay/cs_test_abc", "id": "cs_test_abc"}

    def fake_create_portal_session(*, customer_id, **kwargs):
        calls["portal"].append({"customer_id": customer_id})
        return {"url": "https://billing.stripe.com/p/session/bps_test_xyz", "id": "bps_test_xyz"}

    monkeypatch.setattr(stripe_billing, "create_customer", fake_create_customer)
    monkeypatch.setattr(stripe_billing, "create_checkout_session", fake_create_checkout_session)
    monkeypatch.setattr(stripe_billing, "create_portal_session", fake_create_portal_session)
    return calls


# ---------- is_configured / is_webhook_configured ---------- #


def test_is_configured_true_when_both_set():
    assert stripe_billing.is_configured() is True


def test_is_configured_false_when_secret_missing(monkeypatch):
    monkeypatch.delenv("LIGHTSEI_STRIPE_SECRET_KEY")
    assert stripe_billing.is_configured() is False


def test_is_configured_false_when_price_missing(monkeypatch):
    monkeypatch.delenv("LIGHTSEI_STRIPE_PRICE_ID")
    assert stripe_billing.is_configured() is False


def test_is_webhook_configured_independent_of_main_config(monkeypatch):
    """Webhook secret is a separate dashboard step (create endpoint
    first, copy secret second) so it's checked separately."""
    monkeypatch.delenv("LIGHTSEI_STRIPE_SECRET_KEY")
    monkeypatch.delenv("LIGHTSEI_STRIPE_PRICE_ID")
    assert stripe_billing.is_webhook_configured() is True
    monkeypatch.delenv("LIGHTSEI_STRIPE_WEBHOOK_SECRET")
    assert stripe_billing.is_webhook_configured() is False


# ---------- /workspaces/me/billing/checkout ---------- #


def test_checkout_creates_customer_then_returns_url(client, alice, monkeypatch):
    """First checkout call creates the Stripe customer + stamps the id
    onto the workspace, then returns the Checkout URL."""
    calls = _stub_stripe_calls(monkeypatch)
    token = alice["session_token"]
    workspace_id = alice["workspace"]["id"]

    r = client.post("/workspaces/me/billing/checkout", headers=auth_headers(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["checkout_url"].startswith("https://checkout.stripe.com/")
    assert body["session_id"] == "cs_test_abc"

    assert len(calls["create_customer"]) == 1
    assert calls["create_customer"][0]["workspace_id"] == workspace_id
    assert len(calls["checkout"]) == 1

    # Customer id should be persisted so subsequent checkout calls
    # reuse it.
    with session_scope() as s:
        ws = s.get(Workspace, workspace_id)
        assert ws.stripe_customer_id == "cus_test_123"


def test_checkout_reuses_existing_customer(client, alice, monkeypatch):
    """Second checkout doesn't re-create the Stripe customer."""
    calls = _stub_stripe_calls(monkeypatch)
    token = alice["session_token"]
    workspace_id = alice["workspace"]["id"]

    with session_scope() as s:
        ws = s.get(Workspace, workspace_id)
        ws.stripe_customer_id = "cus_already_exists"

    r = client.post("/workspaces/me/billing/checkout", headers=auth_headers(token))
    assert r.status_code == 200
    assert len(calls["create_customer"]) == 0
    assert calls["checkout"][0]["customer_id"] == "cus_already_exists"


def test_checkout_503_when_not_configured(client, alice, monkeypatch):
    monkeypatch.delenv("LIGHTSEI_STRIPE_SECRET_KEY")
    r = client.post(
        "/workspaces/me/billing/checkout",
        headers=auth_headers(alice["session_token"]),
    )
    assert r.status_code == 503
    assert "not configured" in r.json()["detail"].lower()


def test_checkout_400_when_already_paid(client, alice, monkeypatch):
    """Paid workspaces should use the portal, not Checkout."""
    _stub_stripe_calls(monkeypatch)
    with session_scope() as s:
        ws = s.get(Workspace, alice["workspace"]["id"])
        ws.plan_tier = "paid"
        ws.stripe_customer_id = "cus_paid"
        ws.stripe_subscription_id = "sub_paid"

    r = client.post(
        "/workspaces/me/billing/checkout",
        headers=auth_headers(alice["session_token"]),
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "already_paid"


def test_checkout_502_on_stripe_api_error(client, alice, monkeypatch):
    """Stripe transient failure → 502 with a generic message (don't
    leak the raw stripe exception to the user)."""
    def boom(**kwargs):
        raise stripe_billing.StripeApiError("network timeout")

    monkeypatch.setattr(stripe_billing, "create_customer", lambda **kw: "cus_x")
    monkeypatch.setattr(stripe_billing, "create_checkout_session", boom)

    r = client.post(
        "/workspaces/me/billing/checkout",
        headers=auth_headers(alice["session_token"]),
    )
    assert r.status_code == 502
    assert r.json()["detail"]["error"] == "stripe_error"


# ---------- /workspaces/me/billing/portal ---------- #


def test_portal_returns_url(client, alice, monkeypatch):
    calls = _stub_stripe_calls(monkeypatch)
    with session_scope() as s:
        ws = s.get(Workspace, alice["workspace"]["id"])
        ws.stripe_customer_id = "cus_for_portal"

    r = client.post(
        "/workspaces/me/billing/portal",
        headers=auth_headers(alice["session_token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["portal_url"].startswith("https://billing.stripe.com/")
    assert calls["portal"][0]["customer_id"] == "cus_for_portal"


def test_portal_400_when_no_customer_yet(client, alice, monkeypatch):
    """User has never been to Checkout → no stripe_customer_id → 400
    so the dashboard can show "upgrade first" instead of a broken link."""
    _stub_stripe_calls(monkeypatch)
    r = client.post(
        "/workspaces/me/billing/portal",
        headers=auth_headers(alice["session_token"]),
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "no_customer"


# ---------- /billing/stripe/webhook ---------- #


def _post_webhook(client, event_dict):
    """POST a fake event. construct_webhook_event is stubbed per test
    so the signature header content doesn't matter."""
    return client.post(
        "/billing/stripe/webhook",
        content=json.dumps(event_dict).encode("utf-8"),
        headers={"stripe-signature": "t=0,v1=stub", "content-type": "application/json"},
    )


def test_webhook_checkout_completed_flips_to_paid(client, alice, monkeypatch):
    """checkout.session.completed → plan_tier='paid' + subscription_id
    stamped onto the workspace."""
    workspace_id = alice["workspace"]["id"]

    def fake_construct(*, payload, signature_header):
        return {
            "id": "evt_test_1",
            "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_test_1",
                "client_reference_id": workspace_id,
                "subscription": "sub_brand_new",
                "customer": "cus_test_1",
            }},
        }

    monkeypatch.setattr(stripe_billing, "construct_webhook_event", fake_construct)

    r = _post_webhook(client, {"type": "checkout.session.completed"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"

    with session_scope() as s:
        ws = s.get(Workspace, workspace_id)
        assert ws.plan_tier == "paid"
        assert ws.stripe_subscription_id == "sub_brand_new"


def test_webhook_subscription_deleted_downgrades(client, alice, monkeypatch):
    """customer.subscription.deleted → plan_tier back to 'free'."""
    workspace_id = alice["workspace"]["id"]
    with session_scope() as s:
        ws = s.get(Workspace, workspace_id)
        ws.plan_tier = "paid"
        ws.stripe_customer_id = "cus_test_d"
        ws.stripe_subscription_id = "sub_to_delete"

    def fake_construct(*, payload, signature_header):
        return {
            "id": "evt_test_2",
            "type": "customer.subscription.deleted",
            "data": {"object": {
                "id": "sub_to_delete",
                "customer": "cus_test_d",
                "metadata": {"workspace_id": workspace_id},
            }},
        }

    monkeypatch.setattr(stripe_billing, "construct_webhook_event", fake_construct)

    r = _post_webhook(client, {})
    assert r.status_code == 200, r.text

    with session_scope() as s:
        ws = s.get(Workspace, workspace_id)
        assert ws.plan_tier == "free"
        assert ws.stripe_subscription_id is None


def test_webhook_subscription_past_due_downgrades(client, alice, monkeypatch):
    """customer.subscription.updated with status='past_due' → plan_tier
    back to 'free' so the paywall starts firing until payment is fixed.
    Active vs past_due is the most common churn signal."""
    workspace_id = alice["workspace"]["id"]
    with session_scope() as s:
        ws = s.get(Workspace, workspace_id)
        ws.plan_tier = "paid"
        ws.stripe_customer_id = "cus_test_pd"
        ws.stripe_subscription_id = "sub_past_due"

    def fake_construct(*, payload, signature_header):
        return {
            "id": "evt_test_3",
            "type": "customer.subscription.updated",
            "data": {"object": {
                "id": "sub_past_due",
                "status": "past_due",
                "customer": "cus_test_pd",
                "metadata": {"workspace_id": workspace_id},
            }},
        }

    monkeypatch.setattr(stripe_billing, "construct_webhook_event", fake_construct)

    r = _post_webhook(client, {})
    assert r.status_code == 200

    with session_scope() as s:
        ws = s.get(Workspace, workspace_id)
        assert ws.plan_tier == "free"


def test_webhook_bad_signature_400(client, monkeypatch):
    """construct_webhook_event raises WebhookSignatureError → 400.
    Critically NOT a 5xx, which would make Stripe retry forever."""
    def fake_construct(*, payload, signature_header):
        raise stripe_billing.WebhookSignatureError("bad sig")

    monkeypatch.setattr(stripe_billing, "construct_webhook_event", fake_construct)

    r = _post_webhook(client, {"foo": "bar"})
    assert r.status_code == 400
    assert "bad signature" in r.json()["detail"]


def test_webhook_duplicate_delivery_is_idempotent(client, alice, monkeypatch):
    """Stripe can deliver the same event twice. We re-derive plan_tier
    from the event payload on every delivery, so the second delivery
    is a no-op — same workspace, same plan_tier, same subscription_id."""
    workspace_id = alice["workspace"]["id"]

    def fake_construct(*, payload, signature_header):
        return {
            "id": "evt_test_4",
            "type": "checkout.session.completed",
            "data": {"object": {
                "client_reference_id": workspace_id,
                "subscription": "sub_dupe_test",
                "customer": "cus_dupe",
            }},
        }

    monkeypatch.setattr(stripe_billing, "construct_webhook_event", fake_construct)

    r1 = _post_webhook(client, {})
    r2 = _post_webhook(client, {})
    assert r1.status_code == 200
    assert r2.status_code == 200

    with session_scope() as s:
        ws = s.get(Workspace, workspace_id)
        assert ws.plan_tier == "paid"
        assert ws.stripe_subscription_id == "sub_dupe_test"


def test_webhook_unknown_event_type_acks_with_200(client, monkeypatch):
    """Stripe might be configured to send 'all events.' We don't 4xx the
    ones we don't care about — that would make Stripe pile up retries."""
    def fake_construct(*, payload, signature_header):
        return {
            "id": "evt_test_5",
            "type": "customer.tax_id.created",
            "data": {"object": {}},
        }

    monkeypatch.setattr(stripe_billing, "construct_webhook_event", fake_construct)

    r = _post_webhook(client, {})
    assert r.status_code == 200
    assert r.json()["status"] == "ignored"


def test_webhook_unknown_workspace_acks_with_200(client, monkeypatch):
    """Event for a workspace we don't recognise (test events, deleted
    workspace) → 200 + status='ignored' so Stripe stops retrying."""
    def fake_construct(*, payload, signature_header):
        return {
            "id": "evt_test_6",
            "type": "checkout.session.completed",
            "data": {"object": {
                "client_reference_id": "ws-does-not-exist",
                "subscription": "sub_x",
                "customer": "cus_x",
            }},
        }

    monkeypatch.setattr(stripe_billing, "construct_webhook_event", fake_construct)

    r = _post_webhook(client, {})
    assert r.status_code == 200
    assert r.json()["status"] == "ignored"


def test_webhook_falls_back_to_customer_lookup_when_metadata_missing(
    client, alice, monkeypatch,
):
    """Subscription events created before we started stamping the
    workspace_id metadata still resolve via stripe_customer_id on the
    workspace row. Defensive fallback for the upgrade path."""
    workspace_id = alice["workspace"]["id"]
    with session_scope() as s:
        ws = s.get(Workspace, workspace_id)
        ws.stripe_customer_id = "cus_old_sub"
        ws.plan_tier = "paid"
        ws.stripe_subscription_id = "sub_pre_metadata"

    def fake_construct(*, payload, signature_header):
        return {
            "id": "evt_test_7",
            "type": "customer.subscription.deleted",
            "data": {"object": {
                "id": "sub_pre_metadata",
                "customer": "cus_old_sub",
                # No metadata.workspace_id — covers the older subs.
            }},
        }

    monkeypatch.setattr(stripe_billing, "construct_webhook_event", fake_construct)

    r = _post_webhook(client, {})
    assert r.status_code == 200

    with session_scope() as s:
        ws = s.get(Workspace, workspace_id)
        assert ws.plan_tier == "free"


def test_webhook_returns_400_when_secret_not_set(client, monkeypatch):
    """Misconfigured deployment → 400 (not 5xx) so Stripe gives up
    rather than retrying forever against a broken endpoint."""
    monkeypatch.delenv("LIGHTSEI_STRIPE_WEBHOOK_SECRET")
    r = _post_webhook(client, {"foo": "bar"})
    assert r.status_code == 400
