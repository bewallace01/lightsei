"""Phase 19.3: Slack events webhook tests.

Three surfaces:

1. `backend/slack_events.py` pure helpers — signature verification,
   timestamp tolerance, error paths.
2. `POST /slack/events` URL-verification handshake — Slack pings the
   endpoint at config time with a challenge string we echo.
3. `POST /slack/events` event routing — signature verification,
   idempotency via slack_events PK, app_mention → generation_jobs
   queue, unknown event types acknowledged + ignored.

Signatures are computed for real (HMAC-SHA256 over `v0:{ts}:{body}`)
against a stub signing secret so the test exercises the actual
verification path, not a mock.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text

import secrets_crypto
import slack_events as se
from db import session_scope
from models import (
    SlackEvent,
    SlackWorkspace,
    Workspace,
)
from tests.conftest import auth_headers


SIGNING_SECRET = "fake_slack_signing_secret_for_tests"


@pytest.fixture(autouse=True)
def _slack_signing_env(monkeypatch):
    monkeypatch.setenv("LIGHTSEI_SLACK_SIGNING_SECRET", SIGNING_SECRET)


# ---------- helpers ---------- #


def _sign(body: bytes, timestamp: int) -> str:
    """Build a valid X-Slack-Signature header for the given body + ts."""
    base = b"v0:" + str(timestamp).encode("ascii") + b":" + body
    return "v0=" + hmac.new(
        SIGNING_SECRET.encode("utf-8"),
        base,
        hashlib.sha256,
    ).hexdigest()


def _post(client, body_dict: dict, *, timestamp: int | None = None, signature: str | None = None):
    """POST with a properly-signed body unless timestamp/signature are
    overridden explicitly."""
    body = json.dumps(body_dict).encode("utf-8")
    ts = timestamp if timestamp is not None else int(time.time())
    sig = signature if signature is not None else _sign(body, ts)
    return client.post(
        "/slack/events",
        content=body,
        headers={
            "x-slack-request-timestamp": str(ts),
            "x-slack-signature": sig,
            "content-type": "application/json",
        },
    )


def _install_slack_workspace(lightsei_workspace_id: str, slack_team_id: str = "T_TEST") -> str:
    """Insert a slack_workspaces row for the test workspace so the
    events handler can resolve slack_team_id → lightsei_workspace_id."""
    with session_scope() as s:
        s.add(SlackWorkspace(
            slack_team_id=slack_team_id,
            lightsei_workspace_id=lightsei_workspace_id,
            team_name="Test",
            bot_token_encrypted=secrets_crypto.encrypt("xoxb-fake").encode("ascii"),
            bot_user_id="U0BOT",
            installed_at=datetime.now(timezone.utc),
        ))
    return slack_team_id


# ---------- Pure-helper tests (slack_events module) ---------- #


def test_is_signing_configured_true_when_set():
    assert se.is_signing_configured() is True


def test_is_signing_configured_false_when_unset(monkeypatch):
    monkeypatch.delenv("LIGHTSEI_SLACK_SIGNING_SECRET")
    assert se.is_signing_configured() is False


def test_verify_signature_happy_path():
    body = b'{"hello": "world"}'
    ts = int(time.time())
    sig = _sign(body, ts)
    # Should not raise.
    se.verify_signature(
        body=body, timestamp_header=str(ts), signature_header=sig,
    )


def test_verify_signature_rejects_mismatch():
    body = b'{"hello": "world"}'
    ts = int(time.time())
    with pytest.raises(se.SlackSignatureError) as exc:
        se.verify_signature(
            body=body,
            timestamp_header=str(ts),
            signature_header="v0=deadbeef" * 8,
        )
    assert "signature mismatch" in str(exc.value)


def test_verify_signature_rejects_stale_timestamp():
    body = b"{}"
    # Pretend the request is 10 minutes old (window is 5).
    ts = int(time.time()) - 600
    sig = _sign(body, ts)
    with pytest.raises(se.SlackSignatureError) as exc:
        se.verify_signature(
            body=body,
            timestamp_header=str(ts),
            signature_header=sig,
        )
    assert "tolerance window" in str(exc.value)


def test_verify_signature_rejects_missing_headers():
    body = b"{}"
    with pytest.raises(se.SlackSignatureError):
        se.verify_signature(
            body=body,
            timestamp_header=None,
            signature_header="v0=anything",
        )
    with pytest.raises(se.SlackSignatureError):
        se.verify_signature(
            body=body,
            timestamp_header=str(int(time.time())),
            signature_header=None,
        )


def test_verify_signature_rejects_non_integer_timestamp():
    body = b"{}"
    sig = _sign(body, 0)
    with pytest.raises(se.SlackSignatureError):
        se.verify_signature(
            body=body,
            timestamp_header="not-a-number",
            signature_header=sig,
        )


def test_verify_signature_400_when_secret_unset(monkeypatch):
    monkeypatch.delenv("LIGHTSEI_SLACK_SIGNING_SECRET")
    with pytest.raises(se.SlackSignatureError):
        se.verify_signature(
            body=b"{}",
            timestamp_header=str(int(time.time())),
            signature_header="v0=anything",
        )


# ---------- URL verification handshake ---------- #


def test_url_verification_echoes_challenge(client):
    """Slack pings the endpoint at config time with a challenge. We
    echo it. The handshake bypasses signing-secret config because the
    endpoint might not yet be in env when Slack first pings it."""
    body = json.dumps({"type": "url_verification", "challenge": "abc123xyz"}).encode("utf-8")
    ts = int(time.time())
    # Sign anyway — Slack does sign these requests; our handler just
    # short-circuits the signature check on url_verification because
    # the handshake is what registers the endpoint in the first place.
    sig = _sign(body, ts)
    r = client.post(
        "/slack/events",
        content=body,
        headers={
            "x-slack-request-timestamp": str(ts),
            "x-slack-signature": sig,
            "content-type": "application/json",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"challenge": "abc123xyz"}


def test_url_verification_works_without_signing_secret(client, monkeypatch):
    """Even with no signing secret configured, the handshake works —
    Slack needs to be able to register the endpoint before we've
    pasted the signing secret into env."""
    monkeypatch.delenv("LIGHTSEI_SLACK_SIGNING_SECRET")
    r = client.post(
        "/slack/events",
        content=json.dumps({"type": "url_verification", "challenge": "xyz"}).encode("utf-8"),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json() == {"challenge": "xyz"}


def test_url_verification_missing_challenge_400(client):
    """A url_verification body without the challenge field is malformed
    on Slack's side — surface a 400 so they notice."""
    body = json.dumps({"type": "url_verification"}).encode("utf-8")
    ts = int(time.time())
    r = client.post(
        "/slack/events",
        content=body,
        headers={
            "x-slack-request-timestamp": str(ts),
            "x-slack-signature": _sign(body, ts),
            "content-type": "application/json",
        },
    )
    assert r.status_code == 400


# ---------- Signature verification on real events ---------- #


def test_events_endpoint_400_on_bad_signature(client, alice):
    """Forged delivery → 400 (NOT 5xx, which would make Slack retry
    forever)."""
    _install_slack_workspace(alice["workspace"]["id"])
    r = _post(
        client,
        {
            "type": "event_callback",
            "event": {"type": "app_mention", "text": "@Lightsei hi"},
            "team_id": "T_TEST",
            "event_id": "Ev_BAD_SIG",
        },
        signature="v0=" + "00" * 32,
    )
    assert r.status_code == 400
    assert "bad signature" in r.json()["detail"]


def test_events_endpoint_400_on_missing_signing_secret(client, monkeypatch):
    monkeypatch.delenv("LIGHTSEI_SLACK_SIGNING_SECRET")
    body = json.dumps({"type": "event_callback", "event": {}, "team_id": "T_X", "event_id": "Ev_X"}).encode("utf-8")
    r = client.post(
        "/slack/events",
        content=body,
        headers={
            "x-slack-request-timestamp": str(int(time.time())),
            "x-slack-signature": "v0=anything",
            "content-type": "application/json",
        },
    )
    assert r.status_code == 400


# ---------- Idempotency ---------- #


def test_duplicate_event_id_is_no_op(client, alice):
    """Slack retries delivery; a duplicate event_id should be ignored
    so the orchestrator job isn't enqueued twice."""
    _install_slack_workspace(alice["workspace"]["id"])
    payload = {
        "type": "event_callback",
        "event": {
            "type": "app_mention",
            "text": "@Lightsei hi",
            "channel": "C123",
            "user": "U456",
        },
        "team_id": "T_TEST",
        "event_id": "Ev_DUPLICATE",
    }
    r1 = _post(client, payload)
    assert r1.status_code == 200
    assert r1.json()["status"] == "queued"

    r2 = _post(client, payload)
    assert r2.status_code == 200
    assert r2.json()["status"] == "duplicate"


# ---------- app_mention routing ---------- #


def test_app_mention_enqueues_orchestration_job(client, alice):
    """An app_mention event with a known Slack workspace should land
    on the generation_jobs queue as kind 'slack_orchestration' with
    the right payload."""
    _install_slack_workspace(alice["workspace"]["id"])

    r = _post(
        client,
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "text": "@Lightsei pull our MRR",
                "channel": "C0DATA",
                "user": "U0CSM",
                "ts": "1779100000.000100",
            },
            "team_id": "T_TEST",
            "event_id": "Ev_MENTION_001",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["type"] == "app_mention"
    job_id = body["job_id"]

    # Inspect the row.
    with session_scope() as s:
        row = s.execute(
            text("SELECT kind, status, workspace_id, request_payload FROM generation_jobs WHERE id = :id"),
            {"id": job_id},
        ).mappings().first()
        assert row is not None
        assert row["kind"] == "slack_orchestration"
        assert row["status"] == "pending"
        assert row["workspace_id"] == alice["workspace"]["id"]
        payload = row["request_payload"]
        assert payload["slack_team_id"] == "T_TEST"
        assert payload["channel_id"] == "C0DATA"
        assert payload["text"] == "@Lightsei pull our MRR"
        assert payload["slack_event_id"] == "Ev_MENTION_001"


def test_event_ignored_when_slack_workspace_not_installed(client):
    """If Slack sends an event for a team we don't have a
    slack_workspaces row for (revoked install, never installed), we
    ack 200 + ignore. No job enqueued."""
    r = _post(
        client,
        {
            "type": "event_callback",
            "event": {"type": "app_mention", "text": "hi", "channel": "C", "user": "U"},
            "team_id": "T_NOT_INSTALLED",
            "event_id": "Ev_GHOST",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ignored"
    assert body["reason"] == "slack_workspace_not_installed"


def test_event_ignored_when_install_revoked(client, alice):
    """A revoked install (revoked_at != null) is treated the same as
    no install — events get acked + ignored."""
    _install_slack_workspace(alice["workspace"]["id"], "T_REVOKED")
    with session_scope() as s:
        row = s.get(SlackWorkspace, "T_REVOKED")
        row.revoked_at = datetime.now(timezone.utc)

    r = _post(
        client,
        {
            "type": "event_callback",
            "event": {"type": "app_mention", "text": "hi", "channel": "C", "user": "U"},
            "team_id": "T_REVOKED",
            "event_id": "Ev_REVOKED",
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "ignored"


def test_unknown_event_type_ignored(client, alice):
    """Slack sometimes sends events we didn't subscribe to (membership
    changes, etc.). We ack + ignore so they don't pile up retries."""
    _install_slack_workspace(alice["workspace"]["id"])
    r = _post(
        client,
        {
            "type": "event_callback",
            "event": {"type": "member_joined_channel", "channel": "C"},
            "team_id": "T_TEST",
            "event_id": "Ev_OTHER",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ignored"
    assert body["type"] == "member_joined_channel"


def test_missing_envelope_fields_acked(client):
    """Malformed envelope (no event_id) is acked + ignored — don't
    retry Slack into a loop on a permanently-broken payload."""
    body = json.dumps({"type": "event_callback", "event": {"type": "app_mention"}}).encode("utf-8")
    ts = int(time.time())
    r = client.post(
        "/slack/events",
        content=body,
        headers={
            "x-slack-request-timestamp": str(ts),
            "x-slack-signature": _sign(body, ts),
            "content-type": "application/json",
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "ignored"
