"""Phase 21.2: helper module for the public widget chat surface.

The endpoints live in `main.py` alongside the other route definitions
(same pattern as the Slack endpoints). This module owns the pure
helpers they share:

- `resolve_workspace_by_public_id` — look up a workspace by its
  `widget_public_id`, including the live `customer_facing_agent_name`
  pointer needed to start a new conversation.
- `check_widget_origin` — Origin-header enforcement against the
  workspace's `allowed_widget_origins` allowlist.
- `widget_message_rate_limit_keys` — produces the per-conversation +
  per-workspace rate-limit keys consumed by `limits.rate_limit`.
- `ensure_widget_public_id` — operator-callable helper that mints
  + persists a `widget_public_id` on first access. Used by 21.7's
  settings endpoint; 21.2's public endpoint never mints (only
  reads).

Kept separate from the route handlers because 21.6's
widget_orchestrator + 21.7's settings endpoint will reuse some of
these (origin check at orchestrator side; public-id minter at
settings side).
"""
from __future__ import annotations

import secrets
import os
from typing import Optional
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Workspace


WIDGET_PUBLIC_ID_LEN = 22  # token_urlsafe(16) → ~22 chars; URL-safe, no padding
WIDGET_EMBED_ORIGIN_HEADER = "x-lightsei-embed-origin"


# ---------- Workspace lookup ---------- #


def resolve_workspace_by_public_id(
    session: Session, public_id: str,
) -> Workspace:
    """Find the workspace whose `widget_public_id` matches `public_id`.

    Raises 404 if no match. The lookup is unique by index (Phase
    21.1's `ix_workspaces_widget_public_id`); a missing match means
    either the customer pasted the wrong snippet OR the operator
    rotated the public id (a future surface that's not built yet).
    """
    if not public_id:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "widget_not_found",
                "message": "no widget public id supplied",
            },
        )
    row = session.execute(
        select(Workspace).where(Workspace.widget_public_id == public_id).limit(1)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "widget_not_found",
                "message": (
                    "no widget is configured for this public id. The "
                    "operator may have rotated it; ask them to update "
                    "the snippet on the site."
                ),
            },
        )
    return row


# ---------- Origin enforcement ---------- #


def _origin_from_url(value: str) -> Optional[str]:
    """Return scheme://host[:port] for an absolute URL or origin string."""
    try:
        parsed = urlsplit(value)
    except Exception:
        return None
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _trusted_widget_frame_origins() -> set[str]:
    """Origins allowed to serve the iframe that calls the widget API."""
    raw = os.environ.get("LIGHTSEI_WIDGET_FRAME_ORIGINS")
    origins: set[str] = set()
    if raw:
        candidates = [p.strip() for p in raw.split(",") if p.strip()]
    else:
        dashboard = (
            os.environ.get("LIGHTSEI_DASHBOARD_BASE_URL")
            or os.environ.get("LIGHTSEI_DASHBOARD_URL")
            or "https://app.lightsei.com"
        )
        candidates = [dashboard, "http://localhost:3000", "http://127.0.0.1:3000"]
    for candidate in candidates:
        origin = _origin_from_url(candidate)
        if origin:
            origins.add(origin)
    return origins


def check_widget_origin(
    workspace: Workspace,
    origin: Optional[str],
    embed_origin: Optional[str] = None,
) -> None:
    """Raise 403 if the embedding site is not in the workspace allowlist.

    The iframe is served from Lightsei, so browser fetches to the API
    carry the iframe origin in `Origin`. The iframe also sends the
    browser-derived parent origin in `X-Lightsei-Embed-Origin`, and
    that is the value checked against the customer allowlist. The
    custom header is trusted only when the request itself comes from
    a known Lightsei iframe origin.

    Direct non-iframe requests keep the legacy behavior: their own
    `Origin` header is checked against the allowlist. That keeps curl
    and focused endpoint tests simple without allowing another site to
    spoof the iframe header.

    An empty allowlist refuses every request. The operator's first
    job after picking the customer-facing bot is to add the
    customer's site origins; until then the widget can't be embedded.

    A missing effective origin is also refused. Same-origin form posts
    sometimes omit it, but the widget is always cross-origin.
    """
    effective_origin = origin
    if embed_origin:
        frame_origin = _origin_from_url(origin or "")
        if frame_origin not in _trusted_widget_frame_origins():
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "widget_frame_origin_not_allowed",
                    "origin": origin,
                    "message": (
                        "widget embed origin headers are only accepted from "
                        "the Lightsei-hosted iframe."
                    ),
                },
            )
        effective_origin = _origin_from_url(embed_origin)

    if not effective_origin:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "widget_origin_missing",
                "message": (
                    "this endpoint requires an Origin header. The widget "
                    "iframe sets one automatically; bare curl requests "
                    "need to set it explicitly."
                ),
            },
        )

    allowed = list(workspace.allowed_widget_origins or [])
    if effective_origin not in allowed:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "widget_origin_not_allowed",
                "origin": effective_origin,
                "message": (
                    f"origin {effective_origin!r} is not on this workspace's "
                    "widget allowlist. Ask the operator to add it on "
                    "the widget settings page."
                ),
            },
        )


# ---------- Rate-limit keys ---------- #


def widget_message_rate_limit_keys(
    workspace_id: str, conversation_id: Optional[str],
) -> list[tuple[str, int, float]]:
    """Return the (key, limit, window_s) tuples that the message
    POST endpoint should enforce.

    Two layers:

    - Per-conversation: 1 msg / 1 second sliding window. Keeps a
      single browser tab from flooding the bot.
    - Per-workspace: 60 msgs / 60 seconds. Soft ceiling at the
      tenant level; tuned for a busy site with many concurrent
      conversations but well below any reasonable abuse threshold.

    New-conversation requests skip the per-conversation key (no
    conversation_id yet) and only hit the per-workspace ceiling.
    """
    keys: list[tuple[str, int, float]] = []
    if conversation_id:
        keys.append(
            (f"widget_msg_conv:{conversation_id}", 1, 1.0),
        )
    keys.append(
        (f"widget_msg_ws:{workspace_id}", 60, 60.0),
    )
    return keys


# ---------- Public-id minter (used by 21.7 settings page) ---------- #


def ensure_widget_public_id(session: Session, workspace: Workspace) -> str:
    """Return the workspace's widget public id, minting + persisting
    a fresh one on first access.

    Idempotent — operators who hit the settings page twice get the
    same id back. The 21.2 public endpoint reads this column
    read-only; minting is gated on the authenticated settings
    surface so a fresh workspace doesn't get a public id until the
    operator opts in.
    """
    session.refresh(workspace, with_for_update=True)
    if workspace.widget_public_id:
        return workspace.widget_public_id

    # `token_urlsafe(16)` → 22 chars of `[A-Za-z0-9_-]`. Plenty of
    # entropy (128 bits) — collision odds are negligible even at
    # millions of workspaces. The unique index gives us a hard
    # backstop if a collision ever did fire.
    workspace.widget_public_id = secrets.token_urlsafe(16)
    session.flush()
    return workspace.widget_public_id
