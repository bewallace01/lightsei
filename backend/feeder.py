"""Feeder: makes the AI Business Team proactive.

The business personas (website, lead, reputation, marketing, bi, inbox)
are reactive — each one waits for a command and handles it. That leaves
a gap against the product's core promise ("your team is proactive, not
reactive"): nothing happens unless the owner, the dashboard, or another
assistant asks. The feeder closes that gap. It enqueues a persona's
native command on a schedule, with no human in the loop, so the team
surfaces work on its own.

First feeder: the **weekly business digest**. Once per cadence, for each
workspace running the `bi` assistant, the feeder rolls up the last 7 days
of activity (events the other assistants emitted) and enqueues a
`bi.summarize` command carrying that rollup as `data`. The Business
Intelligence assistant turns it into a plain-English weekly summary and
notifies the owner, without anyone asking for it.

Design notes:
  - build_digest_payload() is a pure function (events in, payload out)
    so the rollup logic is unit-testable without a database or clock.
  - enqueue_due_digests() is idempotent within the cadence window: it
    skips a workspace that already received a feeder-sourced bi.summarize
    inside the dedup window. That makes it safe to tick every minute (it
    rides the scheduler loop, which ticks every 60s) and safe to re-run.
  - approval_state='auto_approved': the owner opted into the team when
    they deployed it; a scheduled digest doesn't need a per-fire human
    gate. Same call the cron-trigger scheduler makes in scheduled_run.py.
  - Never raises out of tick(); a missed digest is best-effort, the next
    tick catches up.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger("lightsei.feeder")


# The persona the digest feeds. One assistant, one command kind for now;
# more feeders (e.g. a daily website-health sweep) can follow the same
# shape without touching this one.
DIGEST_AGENT = "bi"
DIGEST_KIND = "bi.summarize"

# Stamped into every feeder-enqueued command's payload so we can tell a
# proactive digest apart from a human/dashboard-requested summarize when
# we dedup (and so the dashboard can badge it "automatic").
DIGEST_SOURCE = "feeder"

# How much history the digest summarizes.
PERIOD_DAYS = 7

# At most one feeder digest per workspace per this window. Slightly under
# the 7-day period so a digest that runs a few hours late one week doesn't
# push the next one out by a full extra day; the cadence stays weekly.
DEDUP_WINDOW = timedelta(days=6, hours=12)

# Command TTL: a deployment outage gets a day to recover before the
# pending digest expires. Matches COMMAND_TTL / the scheduled_run choice.
_COMMAND_TTL = timedelta(hours=24)

# Real event kinds the personas emit (verified against agents/*/bot.py),
# mapped to the friendly counter the BI assistant reads in `highlights`.
# Counting is generic (events_by_kind covers everything); this curated
# set is only what we promote to a headline number.
_NOTABLE_KINDS = {
    "lead.scored": "leads_scored",
    "reputation.analyzed": "reviews_analyzed",
    "website.check_complete": "website_checks",
    "marketing.created": "marketing_drafts",
    "inbox.processed": "inbox_items",
}


def build_digest_payload(
    events: list[dict[str, Any]],
    *,
    period_days: int = PERIOD_DAYS,
    now_iso: Optional[str] = None,
) -> dict[str, Any]:
    """Roll a list of event dicts into a bi.summarize `data` payload.

    Each event dict looks like
    ``{"kind": str, "agent_name": str, "payload": dict, "timestamp": ...}``.

    Pure: no DB, no clock, no I/O. Returns the dict the BI assistant
    analyzes. Crash events (``*.crash``) are counted under events_by_kind
    like anything else, so a noisy week of failures still shows up in the
    summary rather than being silently dropped.
    """
    by_agent: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for ev in events:
        agent = ev.get("agent_name") or "unknown"
        kind = ev.get("kind") or "unknown"
        by_agent[agent] = by_agent.get(agent, 0) + 1
        by_kind[kind] = by_kind.get(kind, 0) + 1

    highlights = {
        label: by_kind[kind]
        for kind, label in _NOTABLE_KINDS.items()
        if by_kind.get(kind)
    }

    return {
        "source": DIGEST_SOURCE,
        "period_days": period_days,
        "as_of": now_iso,
        "total_events": len(events),
        "events_by_assistant": by_agent,
        "events_by_kind": by_kind,
        "highlights": highlights,
    }


def _workspaces_running_bi(session: Session) -> list[str]:
    """Workspaces with the BI assistant deployed.

    Presence of an Agent row named DIGEST_AGENT is enough: the worker
    creates it on first deploy. A workspace that never deployed the BI
    assistant gets no digest (nothing to run it).
    """
    rows = session.execute(
        text(
            "SELECT workspace_id FROM agents WHERE name = :name"
        ),
        {"name": DIGEST_AGENT},
    ).scalars().all()
    return list(rows)


def _has_recent_feeder_digest(
    session: Session, workspace_id: str, now: datetime
) -> bool:
    """True if a feeder-sourced digest was enqueued inside DEDUP_WINDOW.

    Looks at the command payload's ``source`` marker so a human- or
    dashboard-requested bi.summarize never blocks the proactive one.
    Uses a JSONB ``->>`` text match so the check works without loading
    rows into Python.
    """
    floor = now - DEDUP_WINDOW
    found = session.execute(
        text(
            """
            SELECT 1
              FROM commands
             WHERE workspace_id = :ws
               AND agent_name = :agent
               AND kind = :kind
               AND created_at >= :floor
               AND payload ->> 'source' = :source
             LIMIT 1
            """
        ),
        {
            "ws": workspace_id,
            "agent": DIGEST_AGENT,
            "kind": DIGEST_KIND,
            "floor": floor,
            "source": DIGEST_SOURCE,
        },
    ).first()
    return found is not None


def _recent_events(
    session: Session, workspace_id: str, now: datetime, period_days: int
) -> list[dict[str, Any]]:
    """The workspace's events from the last `period_days`, oldest first."""
    floor = now - timedelta(days=period_days)
    rows = session.execute(
        text(
            """
            SELECT kind, agent_name, payload, timestamp
              FROM events
             WHERE workspace_id = :ws
               AND timestamp >= :floor
             ORDER BY timestamp ASC
            """
        ),
        {"ws": workspace_id, "floor": floor},
    ).mappings().all()
    return [dict(r) for r in rows]


def enqueue_digest_for_workspace(
    session: Session,
    workspace_id: str,
    now: datetime,
    *,
    period_days: int = PERIOD_DAYS,
    force: bool = False,
) -> Optional[str]:
    """Enqueue one bi.summarize digest for a workspace.

    Returns the new command id, or None when skipped (dedup window still
    open and not forced). `force=True` bypasses the dedup check — used by
    the on-demand "generate now" endpoint so an owner can pull a digest
    immediately without waiting for the next window.

    Does not commit; the caller owns the transaction.
    """
    if not force and _has_recent_feeder_digest(session, workspace_id, now):
        return None

    events = _recent_events(session, workspace_id, now, period_days)
    data = build_digest_payload(
        events, period_days=period_days, now_iso=now.isoformat()
    )

    cmd_id = str(uuid.uuid4())
    payload = {
        "source": DIGEST_SOURCE,
        "title": "Weekly business digest",
        "data": data,
    }
    session.execute(
        text(
            """
            INSERT INTO commands (
                id, workspace_id, agent_name, kind, payload, status,
                approval_state, approved_at, created_at, expires_at,
                dispatch_chain_id, dispatch_depth
            ) VALUES (
                :id, :ws, :agent, :kind, CAST(:payload AS JSONB), 'pending',
                'auto_approved', :now, :now, :expires,
                :chain, 0
            )
            """
        ),
        {
            "id": cmd_id,
            "ws": workspace_id,
            "agent": DIGEST_AGENT,
            "kind": DIGEST_KIND,
            "payload": _json_dumps(payload),
            "now": now,
            "expires": now + _COMMAND_TTL,
            "chain": cmd_id,  # feeder digest is its own dispatch-chain root
        },
    )
    logger.info(
        "feeder: enqueued %s digest ws=%s cmd=%s events=%d",
        DIGEST_KIND, workspace_id, cmd_id, data["total_events"],
    )
    return cmd_id


def tick(session: Session, now: datetime) -> int:
    """Run one feeder tick. Returns the number of digests enqueued.

    Rides the scheduler loop (called once per scheduler tick). The
    per-workspace dedup window means ticking every 60s still yields at
    most one digest per workspace per week. Best-effort per workspace:
    one workspace's failure never blocks the others.
    """
    if now.tzinfo is None:
        raise ValueError("feeder.tick requires a timezone-aware now")

    enqueued = 0
    for workspace_id in _workspaces_running_bi(session):
        try:
            if enqueue_digest_for_workspace(session, workspace_id, now):
                enqueued += 1
        except Exception:  # noqa: BLE001 — best-effort, keep going
            logger.exception(
                "feeder: failed to enqueue digest for ws=%s", workspace_id
            )
    return enqueued


def _json_dumps(value: Any) -> str:
    import json

    return json.dumps(value)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
