"""Phase 19.2: Slack OAuth backend tests.

Three surfaces:

1. `backend/slack_oauth.py` pure helpers — state generation,
   authorization URL assembly, `is_configured`, `exchange_code_for_token`.
2. `GET /slack/oauth/start` — 503 when unconfigured, persists state,
   returns the authorization URL.
3. `GET /slack/oauth/callback` — stub Slack's token endpoint; assert
   new-install / re-install paths and bot-token encryption roundtrip.

Tests monkeypatch httpx.post inside slack_oauth so they don't hit
Slack's API. Same shape as the Google OAuth tests stubbing
oauth2.googleapis.com.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

import slack_oauth as so
import secrets_crypto
from db import session_scope
from models import (
    SlackOAuthPendingState,
    SlackWorkspace,
)
from tests.conftest import auth_headers


# ---------- conftest-shaped helpers ---------- #


@pytest.fixture(autouse=True)
def _slack_env(monkeypatch):
    """Pre-configure the env vars so is_configured() returns True for
    every test. Tests needing the not-configured path clear them."""
    monkeypatch.setenv("LIGHTSEI_SLACK_CLIENT_ID", "1234567890.12345")
    monkeypatch.setenv("LIGHTSEI_SLACK_CLIENT_SECRET", "fakefakefakefake")
    monkeypatch.setenv(
        "LIGHTSEI_SLACK_REDIRECT_URI",
        "https://api.lightsei.test/slack/oauth/callback",
    )


def _fake_token_response(
    *,
    access_token: str = "xoxb-fake-bot-token",
    bot_user_id: str = "U0BOTUSER",
    team_id: str = "T0CORALCO",
    team_name: str = "Coral",
    scope: str = "app_mentions:read,chat:write,channels:read,users:read",
    ok: bool = True,
    error: str | None = None,
) -> SimpleNamespace:
    """Construct an httpx-shaped Response stub. Slack's token endpoint
    returns 200 with `ok: false` on most errors; we mirror that shape."""
    body: dict[str, Any] = {"ok": ok}
    if ok:
        body.update(
            {
                "access_token": access_token,
                "bot_user_id": bot_user_id,
                "team": {"id": team_id, "name": team_name},
                "scope": scope,
            }
        )
    else:
        body["error"] = error or "invalid_code"
    return SimpleNamespace(
        status_code=200,
        json=lambda: body,
        text=str(body),
    )


# ---------- Pure-helper tests ---------- #


def test_is_configured_true_when_both_set():
    assert so.is_configured() is True


def test_is_configured_false_when_either_missing(monkeypatch):
    monkeypatch.delenv("LIGHTSEI_SLACK_CLIENT_ID")
    assert so.is_configured() is False


def test_new_state_is_url_safe_and_random():
    a = so.new_state()
    b = so.new_state()
    assert a != b
    assert all(c.isalnum() or c in "_-" for c in a)


def test_build_authorization_url_includes_required_params():
    state = "fake_state_token"
    url = so.build_authorization_url(state=state)
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "slack.com"
    assert parsed.path == "/oauth/v2/authorize"
    qs = parse_qs(parsed.query)
    assert qs["client_id"][0] == "1234567890.12345"
    assert qs["state"][0] == state
    assert qs["redirect_uri"][0].endswith("/slack/oauth/callback")
    # All four default bot scopes survive as a comma-separated list.
    scopes = qs["scope"][0].split(",")
    assert set(scopes) == {
        "app_mentions:read",
        "chat:write",
        "channels:read",
        "users:read",
    }


def test_exchange_code_happy_path(monkeypatch):
    monkeypatch.setattr(
        "slack_oauth.httpx.post",
        lambda *a, **kw: _fake_token_response(),
    )
    result = so.exchange_code_for_token(code="code123")
    assert result["access_token"] == "xoxb-fake-bot-token"
    assert result["bot_user_id"] == "U0BOTUSER"
    assert result["team_id"] == "T0CORALCO"
    assert result["team_name"] == "Coral"


def test_exchange_code_slack_returns_ok_false(monkeypatch):
    """Slack's `ok: false` envelope translates to SlackOAuthError."""
    monkeypatch.setattr(
        "slack_oauth.httpx.post",
        lambda *a, **kw: _fake_token_response(ok=False, error="invalid_code"),
    )
    with pytest.raises(so.SlackOAuthError) as exc:
        so.exchange_code_for_token(code="bad_code")
    assert "invalid_code" in str(exc.value)


def test_exchange_code_http_error(monkeypatch):
    """5xx from Slack becomes SlackOAuthError too."""
    import httpx
    monkeypatch.setattr(
        "slack_oauth.httpx.post",
        lambda *a, **kw: SimpleNamespace(
            status_code=503,
            json=lambda: {},
            text="upstream busted",
        ),
    )
    with pytest.raises(so.SlackOAuthError):
        so.exchange_code_for_token(code="anything")


def test_exchange_code_transport_error(monkeypatch):
    """Network blip during the POST becomes SlackOAuthError."""
    import httpx
    def boom(*a, **kw):
        raise httpx.ConnectError("network unreachable")
    monkeypatch.setattr("slack_oauth.httpx.post", boom)
    with pytest.raises(so.SlackOAuthError) as exc:
        so.exchange_code_for_token(code="anything")
    assert "token exchange failed" in str(exc.value)


# ---------- Endpoint tests ---------- #


def test_oauth_start_503_when_not_configured(client, alice, monkeypatch):
    monkeypatch.delenv("LIGHTSEI_SLACK_CLIENT_ID")
    r = client.get(
        "/slack/oauth/start",
        headers=auth_headers(alice["session_token"]),
    )
    assert r.status_code == 503
    assert "not configured" in r.json()["detail"].lower()


def test_oauth_start_persists_state_and_returns_url(client, alice):
    r = client.get(
        "/slack/oauth/start",
        headers=auth_headers(alice["session_token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"]
    assert body["authorization_url"].startswith("https://slack.com/oauth/v2/authorize?")

    # State row should be in the DB with the right workspace + user.
    with session_scope() as s:
        row = s.get(SlackOAuthPendingState, body["state"])
        assert row is not None
        assert row.lightsei_workspace_id == alice["workspace"]["id"]
        assert row.installed_by_user_id == alice["user"]["id"]


def test_oauth_callback_creates_slack_workspace_row(client, alice, monkeypatch):
    """Happy path: state validates, token exchange succeeds, a fresh
    slack_workspaces row lands with the bot token encrypted."""
    # 1. start → grab the state
    start = client.get(
        "/slack/oauth/start",
        headers=auth_headers(alice["session_token"]),
    ).json()
    state = start["state"]

    # 2. stub Slack's token endpoint
    monkeypatch.setattr(
        "slack_oauth.httpx.post",
        lambda *a, **kw: _fake_token_response(team_id="T_NEW_INSTALL"),
    )

    # 3. callback hits — should redirect (303) to the dashboard
    r = client.get(
        f"/slack/oauth/callback?code=abc123&state={state}",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/integrations/slack?installed=true" in r.headers["location"]

    # 4. verify the row + token roundtrips
    with session_scope() as s:
        row = s.get(SlackWorkspace, "T_NEW_INSTALL")
        assert row is not None
        assert row.lightsei_workspace_id == alice["workspace"]["id"]
        assert row.team_name == "Coral"
        assert row.bot_user_id == "U0BOTUSER"
        # Encrypted blob roundtrips back to the original token.
        decoded = row.bot_token_encrypted.decode("ascii")
        assert secrets_crypto.decrypt(decoded) == "xoxb-fake-bot-token"

    # 5. state row should be gone (single-use)
    with session_scope() as s:
        assert s.get(SlackOAuthPendingState, state) is None


def test_oauth_callback_rejects_unknown_state(client):
    """Invalid state → HTML 400 with a guidance message, not a JSON 500."""
    r = client.get(
        "/slack/oauth/callback?code=abc&state=does-not-exist",
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "text/html" in r.headers["content-type"]
    assert "expired" in r.text.lower() or "no longer valid" in r.text.lower()


def test_oauth_callback_handles_user_cancel(client):
    """Slack hands back ?error=access_denied if the user cancelled.
    Surface a friendly HTML page, not a 500."""
    r = client.get(
        "/slack/oauth/callback?error=access_denied",
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "cancelled" in r.text.lower()


def test_oauth_callback_reinstall_updates_existing_row(client, alice, monkeypatch):
    """If a Slack workspace was previously connected and now re-installs
    (e.g. re-authed after a revoke), update the existing row rather
    than insert a duplicate that would violate the partial-unique index."""
    # First install.
    start1 = client.get(
        "/slack/oauth/start",
        headers=auth_headers(alice["session_token"]),
    ).json()
    monkeypatch.setattr(
        "slack_oauth.httpx.post",
        lambda *a, **kw: _fake_token_response(team_id="T_REINSTALL"),
    )
    client.get(
        f"/slack/oauth/callback?code=first&state={start1['state']}",
        follow_redirects=False,
    )

    # Pretend the user revoked + is reinstalling.
    with session_scope() as s:
        row = s.get(SlackWorkspace, "T_REINSTALL")
        row.revoked_at = datetime.now(timezone.utc)

    # Second install — different access_token but same team.
    start2 = client.get(
        "/slack/oauth/start",
        headers=auth_headers(alice["session_token"]),
    ).json()
    monkeypatch.setattr(
        "slack_oauth.httpx.post",
        lambda *a, **kw: _fake_token_response(
            access_token="xoxb-fresh-token",
            team_id="T_REINSTALL",
        ),
    )
    r2 = client.get(
        f"/slack/oauth/callback?code=second&state={start2['state']}",
        follow_redirects=False,
    )
    assert r2.status_code == 303

    with session_scope() as s:
        row = s.get(SlackWorkspace, "T_REINSTALL")
        assert row is not None
        assert row.revoked_at is None  # cleared on reinstall
        decoded = row.bot_token_encrypted.decode("ascii")
        assert secrets_crypto.decrypt(decoded) == "xoxb-fresh-token"

        # Still exactly one row for this team — no duplicate insert.
        rows = s.execute(
            select(SlackWorkspace).where(SlackWorkspace.slack_team_id == "T_REINSTALL")
        ).scalars().all()
        assert len(rows) == 1
