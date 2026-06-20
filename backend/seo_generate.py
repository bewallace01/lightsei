"""Trigger Spica to draft a new SEO page.

The SEO assistant already handles `seo.generate_page`, the audit feeder
already runs `seo.audit` on a schedule, and the /seo page publishes drafts
to a repo — but nothing was enqueueing the generate command, so drafts
never appeared. This is the owner-facing trigger: enqueue a
`seo.generate_page` command for a target keyword; Spica drafts the page
on the worker and emits `seo.page_drafted`, which surfaces on /seo ready
to publish.

Mirrors ask.py: async by nature (the assistant runs on the worker), so we
drop a command and the dashboard polls for the draft event.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

SEO_AGENT = "seo"
GENERATE_KIND = "seo.generate_page"
GENERATE_SOURCE = "dashboard"

_COMMAND_TTL = timedelta(hours=24)
_MAX_KEYWORD_LEN = 200
_MAX_CONTEXT_LEN = 1000
_PAGE_TYPES = ("service", "location", "blog", "landing")


def enqueue_generate_page(
    session: Session,
    workspace_id: str,
    *,
    keyword: str,
    page_type: Optional[str],
    business_context: Optional[str],
    now: datetime,
) -> str:
    """Enqueue a seo.generate_page command for the SEO assistant. Returns the
    command id. Does not commit. Caller should validate the keyword is
    non-empty first; here it's trimmed + length-capped defensively."""
    kw = (keyword or "").strip()[:_MAX_KEYWORD_LEN]
    pt = (page_type or "landing").strip().lower()
    if pt not in _PAGE_TYPES:
        pt = "landing"

    payload = {"source": GENERATE_SOURCE, "keyword": kw, "page_type": pt}
    bc = (business_context or "").strip()[:_MAX_CONTEXT_LEN]
    if bc:
        payload["business_context"] = bc

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
            "id": cmd_id,
            "ws": workspace_id,
            "agent": SEO_AGENT,
            "kind": GENERATE_KIND,
            "payload": json.dumps(payload),
            "now": now,
            "expires": now + _COMMAND_TTL,
            "chain": cmd_id,
        },
    )
    return cmd_id


AUDIT_KIND = "seo.audit"
AUDIT_SOURCE = "dashboard"


def enqueue_audit(
    session: Session, workspace_id: str, *, url: str, now: datetime
) -> str:
    """Enqueue an on-demand seo.audit for a URL (owner clicked "audit now").
    Returns the command id. Does not commit. Caller validates the URL."""
    cmd_id = str(uuid.uuid4())
    payload = {"source": AUDIT_SOURCE, "url": (url or "").strip()}
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
            "id": cmd_id, "ws": workspace_id, "agent": SEO_AGENT,
            "kind": AUDIT_KIND, "payload": json.dumps(payload),
            "now": now, "expires": now + _COMMAND_TTL, "chain": cmd_id,
        },
    )
    return cmd_id


def configured_audit_url(session: Session, workspace_id: str) -> Optional[str]:
    """The site URL the SEO audit feeder is pointed at (None if unset)."""
    import feeder
    return feeder.normalize_website_url(
        feeder.get_feeder_config(session, workspace_id, feeder.FEEDER_SEO_AUDIT).get("url")
    )


def seo_deployed(session: Session, workspace_id: str) -> bool:
    """Whether the SEO assistant exists for this workspace (so the dashboard
    can prompt to add it before the generate command sits unhandled)."""
    row = session.execute(
        text("SELECT 1 FROM agents WHERE workspace_id = :ws AND name = :n"),
        {"ws": workspace_id, "n": SEO_AGENT},
    ).first()
    return row is not None
