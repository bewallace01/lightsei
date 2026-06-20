"""Trigger the Design assistant (Capella) and read its result.

Capella formats/styles content on the worker, so this is async (same shape
as ask.py): enqueue a `design.format` command, then poll for the
`design.formatted` event by command id. Generic on purpose — any surface
(SEO drafts, marketing, a future formatting box) can format content through
it.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

DESIGN_AGENT = "design"
FORMAT_KIND = "design.format"
SOURCE = "dashboard"

_COMMAND_TTL = timedelta(hours=24)
_MAX_CONTENT_LEN = 60_000
CONTENT_TYPES = ("page", "email", "social", "generic")


def enqueue_format(
    session: Session,
    workspace_id: str,
    *,
    content: str,
    content_type: str,
    accent_color: Optional[str] = None,
    instructions: Optional[str] = None,
    now: datetime,
) -> str:
    """Enqueue a design.format command for Capella. Returns the command id.
    Does not commit. Caller validates content is non-empty."""
    ct = (content_type or "generic").strip().lower()
    if ct not in CONTENT_TYPES:
        ct = "generic"
    payload: dict[str, Any] = {
        "source": SOURCE,
        "content": (content or "")[:_MAX_CONTENT_LEN],
        "content_type": ct,
    }
    if accent_color and accent_color.strip():
        payload["accent_color"] = accent_color.strip()[:32]
    if instructions and instructions.strip():
        payload["instructions"] = instructions.strip()[:500]

    cmd_id = str(uuid.uuid4())
    session.execute(
        text(
            """
            INSERT INTO commands (
                id, workspace_id, agent_name, kind, payload, status,
                approval_state, approved_at, created_at, expires_at,
                dispatch_chain_id, dispatch_depth
            ) VALUES (
                :id, :ws, :agent, :kind, CAST(:payload AS JSONB), 'pending',
                'auto_approved', :now, :now, :expires, :chain, 0
            )
            """
        ),
        {
            "id": cmd_id, "ws": workspace_id, "agent": DESIGN_AGENT,
            "kind": FORMAT_KIND, "payload": json.dumps(payload),
            "now": now, "expires": now + _COMMAND_TTL, "chain": cmd_id,
        },
    )
    return cmd_id


def get_result(session: Session, workspace_id: str, command_id: str) -> dict[str, Any]:
    """Poll for a format result. Returns one of:
      {"status": "formatted", "output": str, "content_type": str}
      {"status": "failed", "error": str}
      {"status": "pending"}
    Matches design.formatted (success) / design.crash (failure) by the
    command_id Capella stamps into each event payload. Workspace-scoped."""
    done = session.execute(
        text(
            """
            SELECT payload FROM events
             WHERE workspace_id = :ws AND kind = 'design.formatted'
               AND payload ->> 'command_id' = :cid
             ORDER BY timestamp DESC LIMIT 1
            """
        ),
        {"ws": workspace_id, "cid": command_id},
    ).first()
    if done is not None:
        p = done[0] or {}
        return {"status": "formatted", "output": p.get("output") or "",
                "content_type": p.get("content_type")}

    crash = session.execute(
        text(
            """
            SELECT payload FROM events
             WHERE workspace_id = :ws AND kind = 'design.crash'
               AND payload ->> 'command_id' = :cid
             ORDER BY timestamp DESC LIMIT 1
            """
        ),
        {"ws": workspace_id, "cid": command_id},
    ).first()
    if crash is not None:
        return {"status": "failed", "error": (crash[0] or {}).get("error") or "design failed"}

    return {"status": "pending"}


def design_deployed(session: Session, workspace_id: str) -> bool:
    """Whether the Design assistant exists for this workspace."""
    row = session.execute(
        text("SELECT 1 FROM agents WHERE workspace_id = :ws AND name = :n"),
        {"ws": workspace_id, "n": DESIGN_AGENT},
    ).first()
    return row is not None
