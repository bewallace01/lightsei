"""Phase 19.4: minimal Slack API client wrapper.

Two surfaces:

- `post_message(slack_team_id, channel, text, thread_ts?)` — decrypts the
  workspace's bot token from `slack_workspaces.bot_token_encrypted` and
  calls Slack's `chat.postMessage`. Used by the 19.4 chat orchestrator
  for error-path Slack responses ("this channel isn't connected") and by
  the 19.5 SDK's `lightsei.post_slack` helper for bot responses.

- `SlackClientError` — surfaced when the bot token is missing, when
  Slack returns a non-ok envelope, or when the network call fails.

The client doesn't open its own DB transactions — callers pass an
existing session so the post is part of the same transactional boundary
as whatever business logic triggered it.

Tests stub `httpx.post` so they don't hit Slack.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx
from sqlalchemy.orm import Session as SessionType

import secrets_crypto
from models import SlackWorkspace

logger = logging.getLogger("lightsei.slack_client")


SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"


class SlackClientError(Exception):
    """Raised when a Slack API call fails. Handler in main.py / jobs
    pipeline catches + records on the job row; the user-facing surface
    will see a generic "couldn't post to Slack" rather than a stack
    trace."""


def _decrypt_bot_token(workspace: SlackWorkspace) -> str:
    """Decrypt the bot token. The stored value is ASCII-encoded bytes
    of the base64 blob produced by secrets_crypto.encrypt."""
    encoded = workspace.bot_token_encrypted
    if not encoded:
        raise SlackClientError(
            f"slack workspace {workspace.slack_team_id!r} has no bot token"
        )
    try:
        return secrets_crypto.decrypt(encoded.decode("ascii"))
    except Exception as exc:
        raise SlackClientError(
            f"failed to decrypt bot token for {workspace.slack_team_id!r}"
        ) from exc


def post_message(
    *,
    session: SessionType,
    slack_team_id: str,
    channel: str,
    text: str,
    thread_ts: Optional[str] = None,
) -> dict[str, Any]:
    """Post a message to Slack on the connected workspace's behalf.

    Returns the parsed Slack response on success (`{ok: true, ts:...,
    channel:...}`). Raises SlackClientError on:
    - Slack workspace not installed (or revoked).
    - Bot token missing or undecryptable.
    - Slack returns ok:false.
    - Network failure or 5xx.
    """
    workspace = session.get(SlackWorkspace, slack_team_id)
    if workspace is None or workspace.revoked_at is not None:
        raise SlackClientError(
            f"slack workspace {slack_team_id!r} not installed (or revoked)"
        )

    token = _decrypt_bot_token(workspace)

    body: dict[str, Any] = {"channel": channel, "text": text}
    if thread_ts:
        body["thread_ts"] = thread_ts

    try:
        response = httpx.post(
            SLACK_POST_MESSAGE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=body,
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        logger.exception("slack_client: post_message transport failed")
        raise SlackClientError(f"slack post failed: {exc}") from exc

    if response.status_code >= 400:
        raise SlackClientError(
            f"slack post returned {response.status_code}"
        )

    try:
        parsed = response.json()
    except Exception as exc:
        raise SlackClientError("slack response was not JSON") from exc

    if not parsed.get("ok"):
        # Slack errors include things like `not_in_channel`,
        # `channel_not_found`, `restricted_action`. Log the specific
        # error code but raise a generic SlackClientError so callers
        # can decide whether to surface to the user.
        err = parsed.get("error") or "unknown_error"
        logger.warning("slack_client: chat.postMessage not ok: %s", parsed)
        raise SlackClientError(f"slack rejected post: {err}")

    return parsed
