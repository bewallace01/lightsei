"""Phase 19.2: Slack OAuth helper.

Pure module wrapping Slack's OAuth v2 flow so the request handlers in
main.py stay thin and tests can stub `httpx.post` without touching
real Slack. Mirrors the shape of `google_oauth.py`.

Configured via env:
  - LIGHTSEI_SLACK_CLIENT_ID: from the Slack app's basic-info page.
  - LIGHTSEI_SLACK_CLIENT_SECRET: same place.
  - LIGHTSEI_SLACK_REDIRECT_URI: the backend's callback URL (defaults
    to https://api.lightsei.com/slack/oauth/callback). Must match
    the redirect URI registered on the Slack app exactly.
  - LIGHTSEI_SLACK_SIGNING_SECRET: NOT used here (this module only
    handles OAuth); the webhook handler in 19.3 reads it.

Tests stub `httpx.post` so they don't hit Slack; production needs the
Slack app's OAuth config + scopes registered before /slack/oauth/start
can complete.
"""
from __future__ import annotations

import logging
import os
import secrets as _stdlib_secrets
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

logger = logging.getLogger("lightsei.slack_oauth")


SLACK_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"

# Bot-token scopes the Lightsei Slack app needs. `app_mentions:read`
# is what surfaces @Lightsei mentions to the events webhook (19.3);
# `chat:write` is what the SDK's lightsei.post_slack helper (19.5)
# uses to reply. `channels:read` lets us label channel rows with
# the friendly channel_name on first sight; `users:read` lets us
# label the operator on installed_by audits.
DEFAULT_BOT_SCOPES = [
    "app_mentions:read",
    "chat:write",
    "channels:read",
    "users:read",
]


def _client_id() -> Optional[str]:
    return os.environ.get("LIGHTSEI_SLACK_CLIENT_ID")


def _client_secret() -> Optional[str]:
    return os.environ.get("LIGHTSEI_SLACK_CLIENT_SECRET")


def default_redirect_uri() -> str:
    return os.environ.get(
        "LIGHTSEI_SLACK_REDIRECT_URI",
        "https://api.lightsei.com/slack/oauth/callback",
    )


def is_configured() -> bool:
    """True when the Slack OAuth client is wired. The /slack/oauth/start
    endpoint 503s if not, to fail loud rather than redirecting the
    user into a half-configured flow that errors out on Slack's end
    with a less actionable message."""
    return bool(_client_id() and _client_secret())


def new_state() -> str:
    """Random state token. Server stores it in slack_oauth_pending_states;
    Slack echoes it on callback so we can rebind to the original
    Lightsei workspace + operator."""
    return _stdlib_secrets.token_urlsafe(32)


def build_authorization_url(
    *,
    state: str,
    redirect_uri: Optional[str] = None,
    scopes: Optional[list[str]] = None,
) -> str:
    """Assemble the Slack consent-screen URL.

    Slack OAuth v2 distinguishes `scope` (bot scopes) from `user_scope`
    (user-token scopes). Lightsei only needs bot scopes — we never act
    on behalf of an individual Slack user — so `user_scope` is omitted.
    """
    redirect_uri = redirect_uri or default_redirect_uri()
    params = {
        "client_id": _client_id() or "",
        "redirect_uri": redirect_uri,
        "scope": ",".join(scopes or DEFAULT_BOT_SCOPES),
        "state": state,
    }
    return f"{SLACK_AUTHORIZE_URL}?{urlencode(params)}"


class SlackOAuthError(Exception):
    """Surfaced from `exchange_code_for_token` when the back-channel
    fails — Slack said no, the network broke, the response was
    malformed. The handler in main.py converts to a 400 and the
    dashboard renders a "Slack install didn't complete" state."""


def exchange_code_for_token(
    *,
    code: str,
    redirect_uri: Optional[str] = None,
) -> dict[str, Any]:
    """Exchange the authorization code for a bot token + team metadata.

    Return shape:
        {
            "access_token": "xoxb-...",       # bot token, sensitive
            "bot_user_id": "U123BOT",         # Slack user id of the bot
            "team_id": "T0123ABCD",           # Slack workspace id
            "team_name": "Coral",             # display name
            "scope": "app_mentions:read,...", # echoed back
        }

    Raises SlackOAuthError on any failure.
    """
    if not is_configured():
        raise SlackOAuthError("slack_oauth not configured")

    redirect_uri = redirect_uri or default_redirect_uri()

    try:
        response = httpx.post(
            SLACK_TOKEN_URL,
            data={
                "client_id": _client_id() or "",
                "client_secret": _client_secret() or "",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        logger.exception("slack_oauth: token exchange transport failed")
        raise SlackOAuthError(f"token exchange failed: {exc}") from exc

    if response.status_code >= 400:
        # Slack OAuth errors are usually 200 with `ok: false` in the
        # body; a 4xx/5xx here means something more fundamental broke.
        raise SlackOAuthError(
            f"token exchange returned {response.status_code}"
        )

    try:
        body = response.json()
    except Exception as exc:
        raise SlackOAuthError("token exchange returned malformed JSON") from exc

    # Slack's "ok: false" envelope. The `error` field is short
    # (`invalid_code`, `code_already_used`, etc.) — log it but
    # surface a generic user-facing message.
    if not body.get("ok"):
        err = body.get("error") or "unknown_error"
        logger.warning("slack_oauth: exchange not ok: %s", body)
        raise SlackOAuthError(f"slack rejected exchange: {err}")

    access_token = body.get("access_token")
    bot_user_id = body.get("bot_user_id")
    team = body.get("team") or {}
    team_id = team.get("id")
    team_name = team.get("name") or "Slack workspace"
    scope = body.get("scope") or ""

    if not access_token or not bot_user_id or not team_id:
        raise SlackOAuthError("slack response missing required fields")

    return {
        "access_token": str(access_token),
        "bot_user_id": str(bot_user_id),
        "team_id": str(team_id),
        "team_name": str(team_name),
        "scope": str(scope),
    }
