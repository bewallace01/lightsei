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
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Workspace


WIDGET_PUBLIC_ID_LEN = 22  # token_urlsafe(16) → ~22 chars; URL-safe, no padding


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


def check_widget_origin(workspace: Workspace, origin: Optional[str]) -> None:
    """Raise 403 if `origin` is not in the workspace's allowlist.

    The end user's browser sends an `Origin` header on every
    cross-origin POST; iframes inherit the parent document's origin.
    Allowlist is an exact-match list of `https://host[:port]` strings
    — wildcards punted to a future surface (the v1 settings page
    asks the operator to add one entry per domain).

    An empty allowlist refuses every request. The operator's first
    job after picking the customer-facing bot is to add the
    customer's site origins; until then the widget can't be embedded.

    A missing Origin header is also refused. Same-origin form posts
    sometimes omit it, but the widget is always cross-origin (iframe
    on app.lightsei.com served into a customer's page).
    """
    if not origin:
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
    if origin not in allowed:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "widget_origin_not_allowed",
                "origin": origin,
                "message": (
                    f"origin {origin!r} is not on this workspace's "
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
    if workspace.widget_public_id:
        return workspace.widget_public_id

    # `token_urlsafe(16)` → 22 chars of `[A-Za-z0-9_-]`. Plenty of
    # entropy (128 bits) — collision odds are negligible even at
    # millions of workspaces. The unique index gives us a hard
    # backstop if a collision ever did fire.
    workspace.widget_public_id = secrets.token_urlsafe(16)
    session.flush()
    return workspace.widget_public_id
