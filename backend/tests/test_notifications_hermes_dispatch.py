"""Phase 11.4: POST /workspaces/me/notifications/dispatch — the
backend endpoint Hermes calls when fanning a `hermes.post` command
out to a notification channel by name.

Targets the routing + delivery-row recording specifically; the
channel-side formatters (slack.py et al.) have their own tests in
test_notifications_dispatch.py.
"""
from unittest.mock import patch

from models import NotificationDelivery
from tests.conftest import auth_headers


SLACK_URL = (
    "https://hooks.slack.com/services/T01ABCDEF/B02ABCDEF/abcdef1234567890"
)


def _create_channel(client, h, **overrides):
    body = {
        "name": "primary",
        "type": "slack",
        "target_url": SLACK_URL,
        "triggers": [],
    }
    body.update(overrides)
    r = client.post(
        "/workspaces/me/notifications", json=body, headers=h
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_dispatch_to_named_channel_records_delivery(client, alice):
    """Happy path: existing channel, dispatcher returns success,
    delivery row written with `trigger='hermes.post'` so audit logs
    can tell Hermes traffic apart from regular trigger fires."""
    h = auth_headers(alice["api_key"]["plaintext"])
    _create_channel(client, h, name="ops")

    # The notifications module's dispatch is what actually POSTs to
    # Slack — stub it so the test doesn't make real HTTP calls.
    fake_delivery = type(
        "FakeDelivery",
        (),
        {
            "status": "sent",
            "response_summary": {"http_status": 200},
            "attempt_count": 1,
        },
    )()
    with patch("notifications.dispatch", return_value=fake_delivery):
        r = client.post(
            "/workspaces/me/notifications/dispatch",
            json={
                "channel_name": "ops",
                "text": "✅ atlas: 322 passed",
                "severity": "info",
            },
            headers=h,
        )
    assert r.status_code == 200, r.text
    delivery = r.json()["delivery"]
    assert delivery["status"] == "sent"
    assert delivery["trigger"] == "hermes.post"
    assert delivery["response_summary"]["http_status"] == 200


def test_dispatch_to_missing_channel_404(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    r = client.post(
        "/workspaces/me/notifications/dispatch",
        json={"channel_name": "nope", "text": "x", "severity": "info"},
        headers=h,
    )
    assert r.status_code == 404
    assert "nope" in r.json()["detail"]


def test_dispatch_isolated_across_workspaces(client, alice, bob):
    """Bob can't post to a channel registered in alice's workspace."""
    h_alice = auth_headers(alice["api_key"]["plaintext"])
    h_bob = auth_headers(bob["api_key"]["plaintext"])
    _create_channel(client, h_alice, name="alices-channel")

    r = client.post(
        "/workspaces/me/notifications/dispatch",
        json={
            "channel_name": "alices-channel",
            "text": "hi from bob",
            "severity": "info",
        },
        headers=h_bob,
    )
    assert r.status_code == 404


def test_dispatch_forwards_failed_status_without_5xx(client, alice):
    """A 4xx from the channel comes back as a 200 with a `failed`
    delivery — the endpoint's job is to record what happened, not
    to bubble webhook failures up to its own response code (the
    bot decides whether to retry based on the delivery's status)."""
    h = auth_headers(alice["api_key"]["plaintext"])
    _create_channel(client, h, name="bad-channel")

    fake_delivery = type(
        "FakeDelivery",
        (),
        {
            "status": "failed",
            "response_summary": {"http_status": 401, "body": "invalid_auth"},
            "attempt_count": 1,
        },
    )()
    with patch("notifications.dispatch", return_value=fake_delivery):
        r = client.post(
            "/workspaces/me/notifications/dispatch",
            json={
                "channel_name": "bad-channel",
                "text": "hi",
                "severity": "info",
            },
            headers=h,
        )
    assert r.status_code == 200
    delivery = r.json()["delivery"]
    assert delivery["status"] == "failed"
    assert delivery["response_summary"]["http_status"] == 401


def test_slack_formatter_renders_hermes_post(client, alice):
    """End-to-end against the real slack.format function: post text
    that includes the upstream agent's emoji prefix; the formatter
    should pass it through unchanged inside a Block Kit section."""
    from notifications import slack
    from notifications._types import Signal
    from datetime import datetime, timezone

    signal = Signal(
        trigger="hermes.post",
        agent_name="hermes",
        dashboard_url="https://app.example.com/notifications",
        timestamp=datetime.now(timezone.utc),
        payload={"text": "✅ atlas: 322 passed", "severity": "info"},
        workspace_id="ws-1",
    )
    body = slack.format(signal)
    assert body["text"] == "✅ atlas: 322 passed"
    # The text lives inside a single section block.
    section = body["blocks"][0]
    assert section["type"] == "section"
    assert section["text"]["text"] == "✅ atlas: 322 passed"


def test_dispatch_rejects_invalid_severity(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    r = client.post(
        "/workspaces/me/notifications/dispatch",
        json={
            "channel_name": "any",
            "text": "x",
            "severity": "invalid-thing",
        },
        headers=h,
    )
    assert r.status_code == 422  # Pydantic regex rejection
