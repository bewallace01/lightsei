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

# --- Feeder catalog --- #
# Stable identifiers for each feeder, used as feeder_settings.feeder_kind
# and surfaced in the settings API/UI. These are NOT command kinds (both
# feeders emit bi.summarize); they name the *behavior* so an owner can
# toggle each independently. Adding a feeder = add an entry here, no
# migration (feeder_kind is a plain string column).
FEEDER_WEEKLY_DIGEST = "weekly_digest"
FEEDER_COST_SPIKE = "cost_spike"
FEEDER_INBOX_GMAIL = "inbox_gmail"
FEEDER_REPUTATION_REVIEWS = "reputation_reviews"
FEEDER_WEBSITE_HEALTH = "website_health"
FEEDER_SEO_AUDIT = "seo_audit"

# Each entry carries `default_enabled`: the on/off a workspace gets before
# the owner has toggled anything. The two internal-data feeders default ON
# (they read the workspace's own events/cost). The inbox feeder defaults
# OFF because it polls a real external inbox — that's the owner's call to
# turn on, not something that should start the moment Gmail is connected.
FEEDER_CATALOG = [
    {
        "kind": FEEDER_WEEKLY_DIGEST,
        "name": "Weekly business digest",
        "description": (
            "Your Business Intelligence assistant summarizes the last 7 days "
            "of activity once a week, on its own."
        ),
        "default_enabled": True,
    },
    {
        "kind": FEEDER_COST_SPIKE,
        "name": "Spend spike alert",
        "description": (
            "When weekly LLM spend jumps sharply over the prior week, the "
            "assistant explains what drove it. Stays quiet otherwise."
        ),
        "default_enabled": True,
    },
    {
        "kind": FEEDER_INBOX_GMAIL,
        "name": "Inbox triage (Gmail)",
        "description": (
            "Polls your connected Gmail for new mail and hands each message "
            "to the Inbox assistant to categorize, flag urgency, and draft a "
            "reply. Requires the Gmail connector. Off by default."
        ),
        "default_enabled": False,
    },
    {
        "kind": FEEDER_REPUTATION_REVIEWS,
        "name": "Review monitoring (Google)",
        "description": (
            "Polls your Google Business Profile for new reviews and hands "
            "each to the Reputation assistant to gauge sentiment and draft a "
            "response. Requires the Google Business Profile connector. Off by "
            "default."
        ),
        "default_enabled": False,
        # This feeder takes a target the owner can pick (which business
        # location). The connector backing the picker.
        "target_connector": "google_business",
    },
    {
        "kind": FEEDER_WEBSITE_HEALTH,
        "name": "Website health checks",
        "description": (
            "Checks your website on a schedule (is it up, any broken links, "
            "is the contact form still there) and alerts you only when "
            "something is wrong. Add your site address to start."
        ),
        # Defaults ON: it watches the owner's own public site (nothing
        # private, low rate) and is a no-op until a URL is set, so it can't
        # do anything surprising before the owner provides one.
        "default_enabled": True,
        # This feeder's target is a plain website URL the owner types, not a
        # connector account. The settings UI renders a URL input for it.
        "url_target": True,
    },
    {
        "kind": FEEDER_SEO_AUDIT,
        "name": "SEO checkups",
        "description": (
            "Audits your website's on-page SEO on a schedule (titles, meta "
            "tags, headings, structured data, content) and surfaces a "
            "prioritized list of fixes. Add your site address to start."
        ),
        # Same shape as website health: watches the owner's own public site,
        # no-op until a URL is set. Defaults ON.
        "default_enabled": True,
        "url_target": True,
    },
]

_DEFAULT_ENABLED = {e["kind"]: e["default_enabled"] for e in FEEDER_CATALOG}

# The Inbox feeder's target + dedup constants.
INBOX_AGENT = "inbox"
INBOX_KIND = "inbox.process"
INBOX_SOURCE = "feeder-inbox"
# Gmail query for the poll. Unread, recent, capped — the assistant triages
# a manageable batch per tick rather than the whole mailbox.
INBOX_GMAIL_QUERY = "is:unread newer_than:7d"
INBOX_MAX_PER_TICK = 10

# The Reputation feeder's target + dedup constants.
REPUTATION_AGENT = "reputation"
REPUTATION_KIND = "reputation.check"
REPUTATION_SOURCE = "feeder-reputation"
REPUTATION_MAX_PER_TICK = 20

# The Website feeder's target + dedup constants. Unlike the review/inbox
# feeders (one command per new external item), this is a periodic sweep:
# one website.check per cadence window for the configured URL. The window
# is just under a day so a check that runs a few hours late doesn't push
# the next one out to every-other-day; the cadence stays daily.
WEBSITE_AGENT = "website"
WEBSITE_KIND = "website.check"
WEBSITE_SOURCE = "feeder-website"
WEBSITE_DEDUP_WINDOW = timedelta(hours=20)

# The SEO feeder's target + dedup constants. Same periodic-sweep shape as
# website health: one seo.audit per cadence window for the configured URL.
SEO_AGENT = "seo"
SEO_KIND = "seo.audit"
SEO_SOURCE = "feeder-seo"
SEO_DEDUP_WINDOW = timedelta(hours=20)


def normalize_website_url(raw: Optional[str]) -> Optional[str]:
    """Coerce an owner-typed site address into a fetchable URL, or None if
    it isn't one. Pure + testable.

    Owners type "example.com", "www.example.com", or a full URL. We add a
    scheme when missing (defaulting to https) and require a host with a dot
    so a bare word ("home") or empty string can't become a target. Returns
    the normalized URL string, or None when it can't be made into one.
    """
    import ipaddress
    from urllib.parse import urlparse, urlunparse

    if not raw:
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    if "://" not in candidate:
        candidate = "https://" + candidate
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https"):
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    host = parsed.hostname
    if parsed.username or parsed.password:
        return None
    if not host or "." not in host or host.startswith(".") or host.endswith("."):
        return None
    host_l = host.lower().rstrip(".")
    if host_l == "localhost" or host_l.endswith((".localhost", ".local", ".internal")):
        return None
    try:
        ip = ipaddress.ip_address(host_l.strip("[]"))
    except ValueError:
        pass
    else:
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return None
    # Rebuild from parsed parts so we drop stray whitespace / fragments and
    # store a canonical form. Keep path/query (a contact page may be deep).
    netloc = host if port is None else f"{host}:{port}"
    return urlunparse(
        (parsed.scheme, netloc, parsed.path, "", parsed.query, "")
    )

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

# --- Cost-spike alert (the second feeder) --- #
# Same command kind + target assistant as the digest, distinguished by a
# different source marker so the two dedup independently. The BI assistant
# is the natural home for "why did spend change?" — the alert asks it.
COST_ALERT_SOURCE = "feeder-cost-alert"

# Fire only on a meaningful jump: this week's spend must exceed last week's
# by this ratio. 1.5 = a 50% week-over-week increase. Tuned to surface
# something the owner would actually want to know without crying wolf.
COST_SPIKE_RATIO = 1.5

# Don't compute a ratio off a trivial baseline. A $0.02 -> $0.10 week is a
# 5x "spike" that means nothing; require last week to have had real spend
# before an increase counts as an anomaly.
COST_MIN_PRIOR_USD = 1.0

# How many days each comparison window spans (this week vs the prior week).
COST_WINDOW_DAYS = 7

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
    "seo.audit_complete": "seo_audits",
    "seo.page_drafted": "seo_pages_drafted",
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


def detect_cost_spike(
    this_week_usd: float,
    prior_week_usd: float,
    *,
    ratio_threshold: float = COST_SPIKE_RATIO,
    min_prior_usd: float = COST_MIN_PRIOR_USD,
) -> Optional[dict[str, Any]]:
    """Decide whether a week-over-week spend jump is worth flagging.

    Pure: numbers in, verdict out. Returns an alert dict (the headline +
    the figures the BI assistant explains) when this week's spend exceeds
    last week's by ``ratio_threshold``, or None when it's normal.

    Two guards keep it from crying wolf:
      - ``prior_week_usd`` must be at least ``min_prior_usd`` — a ratio off
        a near-zero baseline is meaningless (a few cents to a dollar is not
        an anomaly, it's a rounding artifact of a quiet week ending).
      - the increase must clear the ratio, not merely be positive.
    """
    if prior_week_usd < min_prior_usd:
        return None
    if this_week_usd <= prior_week_usd * ratio_threshold:
        return None

    pct = round((this_week_usd - prior_week_usd) / prior_week_usd * 100)
    return {
        "this_week_usd": round(this_week_usd, 4),
        "prior_week_usd": round(prior_week_usd, 4),
        "pct_increase": pct,
        "headline": (
            f"LLM spend rose to ${this_week_usd:,.2f} this week, "
            f"up {pct}% from ${prior_week_usd:,.2f} last week."
        ),
    }


def _cost_in_window(
    session: Session, workspace_id: str, start: datetime, end: datetime
) -> tuple[float, dict[str, float]]:
    """Total + per-assistant spend from runs started in [start, end).

    Reads runs.cost_usd directly (same source as the cost dashboard) so a
    spike the owner sees on /cost is the same number the alert reasons
    about.
    """
    rows = session.execute(
        text(
            """
            SELECT agent_name, COALESCE(SUM(cost_usd), 0) AS usd
              FROM runs
             WHERE workspace_id = :ws
               AND started_at >= :start
               AND started_at < :end
             GROUP BY agent_name
            """
        ),
        {"ws": workspace_id, "start": start, "end": end},
    ).mappings().all()
    by_agent = {r["agent_name"]: float(r["usd"]) for r in rows}
    total = round(sum(by_agent.values()), 6)
    return total, by_agent


def enqueue_cost_alert_for_workspace(
    session: Session,
    workspace_id: str,
    now: datetime,
    *,
    force: bool = False,
) -> Optional[str]:
    """Enqueue a bi.summarize cost-spike alert iff spend actually spiked.

    Computes this-week vs prior-week spend, runs detect_cost_spike, and
    only when it returns an alert does it enqueue a BI command asking for a
    short plain-English explanation. Returns the command id, or None when
    there's no spike (or the dedup window is still open and not forced).

    Unlike the weekly digest, this fires on a condition, not a clock — so a
    quiet or steady workspace never gets a command at all. Does not commit.
    """
    if not force and _has_recent_feeder_command(
        session, workspace_id, now, source=COST_ALERT_SOURCE
    ):
        return None

    this_start = now - timedelta(days=COST_WINDOW_DAYS)
    prior_start = now - timedelta(days=COST_WINDOW_DAYS * 2)
    this_total, this_by_agent = _cost_in_window(
        session, workspace_id, this_start, now
    )
    prior_total, prior_by_agent = _cost_in_window(
        session, workspace_id, prior_start, this_start
    )

    alert = detect_cost_spike(this_total, prior_total)
    if alert is None:
        return None

    cmd_id = str(uuid.uuid4())
    payload = {
        "source": COST_ALERT_SOURCE,
        "title": "Spend alert",
        "question": (
            f"{alert['headline']} In 2-3 plain sentences for a non-technical "
            "owner, say what most likely drove the increase (name the "
            "assistant if one stands out in the per-assistant figures) and "
            "whether it looks like something to worry about."
        ),
        "data": {
            "source": COST_ALERT_SOURCE,
            "this_week_usd": alert["this_week_usd"],
            "prior_week_usd": alert["prior_week_usd"],
            "pct_increase": alert["pct_increase"],
            "this_week_by_assistant": this_by_agent,
            "prior_week_by_assistant": prior_by_agent,
        },
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
            "chain": cmd_id,
        },
    )
    logger.info(
        "feeder: cost spike ws=%s this=%.2f prior=%.2f (+%d%%) cmd=%s",
        workspace_id, this_total, prior_total, alert["pct_increase"], cmd_id,
    )
    return cmd_id


def is_feeder_enabled(
    session: Session, workspace_id: str, feeder_kind: str
) -> bool:
    """Whether a feeder is on for a workspace.

    With no feeder_settings row, falls back to the feeder's catalog
    default (the two internal feeders default on, the inbox feeder off), so
    the table stays a pure override and existing behavior needs no
    backfill. A row only exists once the owner toggles.
    """
    row = session.execute(
        text(
            "SELECT enabled FROM feeder_settings "
            "WHERE workspace_id = :ws AND feeder_kind = :kind"
        ),
        {"ws": workspace_id, "kind": feeder_kind},
    ).first()
    if row is None:
        return _DEFAULT_ENABLED.get(feeder_kind, True)
    return bool(row[0])


def get_feeder_settings(
    session: Session, workspace_id: str
) -> list[dict[str, Any]]:
    """The catalog annotated with this workspace's enabled state.

    Powers the settings API/UI: every known feeder, in catalog order, with
    its current on/off (defaulting to on where no row exists).
    """
    rows = session.execute(
        text(
            "SELECT feeder_kind, enabled, config FROM feeder_settings "
            "WHERE workspace_id = :ws"
        ),
        {"ws": workspace_id},
    ).mappings().all()
    enabled_by_kind = {r["feeder_kind"]: bool(r["enabled"]) for r in rows}
    config_by_kind = {r["feeder_kind"]: (r["config"] or {}) for r in rows}
    return [
        {
            "kind": entry["kind"],
            "name": entry["name"],
            "description": entry["description"],
            "enabled": enabled_by_kind.get(
                entry["kind"], entry["default_enabled"]
            ),
            "config": config_by_kind.get(entry["kind"], {}),
            # Whether this feeder takes a connector-backed target the owner
            # picks from a list (drives the location dropdown).
            "targetable": entry.get("target_connector") is not None,
            # Whether this feeder takes a free-text URL target (drives a URL
            # input). The website feeder is the first of these.
            "url_target": bool(entry.get("url_target")),
        }
        for entry in FEEDER_CATALOG
    ]


def get_feeder_config(
    session: Session, workspace_id: str, feeder_kind: str
) -> dict[str, Any]:
    """The feeder's per-workspace config blob ({} if none / no row)."""
    row = session.execute(
        text(
            "SELECT config FROM feeder_settings "
            "WHERE workspace_id = :ws AND feeder_kind = :kind"
        ),
        {"ws": workspace_id, "kind": feeder_kind},
    ).first()
    if row is None or row[0] is None:
        return {}
    return dict(row[0])


def set_feeder_enabled(
    session: Session, workspace_id: str, feeder_kind: str, enabled: bool,
    now: datetime,
) -> None:
    """Upsert a feeder's on/off for a workspace. Does not commit.

    Idempotent: re-setting the same value just bumps updated_at. Leaves
    config untouched (a fresh row gets the table default '{}').
    """
    session.execute(
        text(
            """
            INSERT INTO feeder_settings
                (workspace_id, feeder_kind, enabled, created_at, updated_at)
            VALUES (:ws, :kind, :enabled, :now, :now)
            ON CONFLICT (workspace_id, feeder_kind)
            DO UPDATE SET enabled = :enabled, updated_at = :now
            """
        ),
        {"ws": workspace_id, "kind": feeder_kind, "enabled": enabled,
         "now": now},
    )


def set_feeder_config(
    session: Session, workspace_id: str, feeder_kind: str,
    config: dict[str, Any], now: datetime,
) -> None:
    """Upsert a feeder's config blob for a workspace. Does not commit.

    A fresh row gets `enabled` from the catalog default (NOT the table
    default), so setting a target before turning the feeder on can't
    silently enable an off-by-default feeder (the inbox/reputation case).
    On conflict it touches only config, leaving the owner's on/off alone.
    """
    session.execute(
        text(
            """
            INSERT INTO feeder_settings
                (workspace_id, feeder_kind, enabled, config,
                 created_at, updated_at)
            VALUES (:ws, :kind, :enabled, CAST(:config AS JSONB), :now, :now)
            ON CONFLICT (workspace_id, feeder_kind)
            DO UPDATE SET config = CAST(:config AS JSONB), updated_at = :now
            """
        ),
        {"ws": workspace_id, "kind": feeder_kind,
         "enabled": _DEFAULT_ENABLED.get(feeder_kind, True),
         "config": _json_dumps(config), "now": now},
    )


def _workspaces_running_agent(session: Session, agent_name: str) -> list[str]:
    """Workspaces with the named assistant deployed.

    Presence of an Agent row is enough: the worker creates it on first
    deploy. A workspace that never deployed that assistant gets no feeder
    that targets it (nothing to run the command).
    """
    rows = session.execute(
        text("SELECT workspace_id FROM agents WHERE name = :name"),
        {"name": agent_name},
    ).scalars().all()
    return list(rows)


def enqueue_inbox_items_for_workspace(
    session: Session, workspace_id: str, now: datetime
) -> int:
    """Poll the workspace's Gmail and enqueue one inbox.process per new
    message. Returns the number enqueued.

    Reaches Gmail through invoke_connector_tool — the SAME capability +
    trust-zone + install gates the bot-facing endpoint uses — dispatching
    as the `inbox` assistant. So a workspace where the inbox assistant
    isn't authorized for connector:gmail, or hasn't connected Gmail, simply
    gets nothing: the gate raises, we catch it and return 0. Best-effort by
    design; a connector hiccup never crashes the tick.

    Dedup is on the Gmail message id (permanent — an email is processed
    once, ever), not a time window, so re-polling the same unread thread
    doesn't re-triage it.
    """
    # Lazy import: feeder is imported by the scheduler at startup, main is
    # fully loaded by the time a tick runs. Avoids an import cycle.
    from sqlalchemy import bindparam

    try:
        import main as _main
        result = _main.invoke_connector_tool(
            session,
            workspace_id=workspace_id,
            connector_type="gmail",
            tool_name="search_inbox",
            payload={"query": INBOX_GMAIL_QUERY,
                     "max_results": INBOX_MAX_PER_TICK},
            source_agent=INBOX_AGENT,
        )
    except Exception as exc:  # noqa: BLE001 — gate/connector failure = skip
        logger.info(
            "feeder: inbox poll skipped for ws=%s (%s)",
            workspace_id, type(exc).__name__,
        )
        return 0

    messages = (result or {}).get("messages") or []
    ids = [m.get("id") for m in messages if m.get("id")]
    if not ids:
        return 0

    # Which of these have we already enqueued? One query over the candidate
    # ids, not the whole history.
    seen_stmt = text(
        """
        SELECT payload ->> 'gmail_message_id' AS mid
          FROM commands
         WHERE workspace_id = :ws
           AND kind = :kind
           AND payload ->> 'source' = :source
           AND payload ->> 'gmail_message_id' IN :ids
        """
    ).bindparams(bindparam("ids", expanding=True))
    seen = {
        r[0] for r in session.execute(
            seen_stmt,
            {"ws": workspace_id, "kind": INBOX_KIND,
             "source": INBOX_SOURCE, "ids": ids},
        )
    }

    enqueued = 0
    for m in messages:
        mid = m.get("id")
        if not mid or mid in seen:
            continue
        email = {
            "from": m.get("from"),
            "subject": m.get("subject"),
            # search_inbox returns metadata only; the snippet is the body
            # the assistant triages from. A full fetch (get_thread) per
            # message would be a heavier poll; the snippet categorizes well.
            "body": m.get("snippet") or "",
            "snippet": m.get("snippet") or "",
            "date": m.get("date"),
            "thread_id": m.get("thread_id"),
            "gmail_message_id": mid,
        }
        cmd_id = str(uuid.uuid4())
        payload = {
            "source": INBOX_SOURCE,
            "gmail_message_id": mid,
            "email": email,
        }
        session.execute(
            text(
                """
                INSERT INTO commands (
                    id, workspace_id, agent_name, kind, payload, status,
                    approval_state, approved_at, created_at, expires_at,
                    dispatch_chain_id, dispatch_depth
                ) VALUES (
                    :id, :ws, :agent, :kind, CAST(:payload AS JSONB),
                    'pending', 'auto_approved', :now, :now, :expires,
                    :chain, 0
                )
                """
            ),
            {
                "id": cmd_id,
                "ws": workspace_id,
                "agent": INBOX_AGENT,
                "kind": INBOX_KIND,
                "payload": _json_dumps(payload),
                "now": now,
                "expires": now + _COMMAND_TTL,
                "chain": cmd_id,
            },
        )
        enqueued += 1

    if enqueued:
        logger.info(
            "feeder: enqueued %d inbox item(s) for ws=%s", enqueued, workspace_id
        )
    return enqueued


def _gb_call(session, workspace_id, tool_name, payload):
    """One Google Business Profile call through the gated connector path,
    dispatched as the reputation assistant. Returns the result dict, or
    None on any gate/connector failure (so the caller skips cleanly)."""
    import main as _main
    try:
        return _main.invoke_connector_tool(
            session,
            workspace_id=workspace_id,
            connector_type="google_business",
            tool_name=tool_name,
            payload=payload,
            source_agent=REPUTATION_AGENT,
        )
    except Exception as exc:  # noqa: BLE001 — gate/connector failure = skip
        logger.info(
            "feeder: reputation %s skipped for ws=%s (%s)",
            tool_name, workspace_id, type(exc).__name__,
        )
        return None


def enqueue_reputation_reviews_for_workspace(
    session: Session, workspace_id: str, now: datetime
) -> int:
    """Poll Google Business Profile reviews and enqueue one
    reputation.check per new review. Returns the number enqueued.

    Targeting: if the owner has configured an account + location (via the
    settings UI), that target is used directly — no discovery calls. With
    no config, falls back to auto-discovering the first account's first
    location, which covers the common single-location small business with
    zero setup. Either way the chosen location id rides each command's
    payload, so the target is never silent.

    Reaches the connector through invoke_connector_tool — the same
    capability + trust-zone + install gates as the bot-facing endpoint —
    dispatching as the reputation assistant. A workspace that hasn't
    connected Google Business Profile (or isn't authorized) gets nothing.

    Dedup is permanent on the review id: a review is analyzed once, ever.
    """
    config = get_feeder_config(session, workspace_id, FEEDER_REPUTATION_REVIEWS)
    account_id = config.get("account_id")
    location_id = config.get("location_id")
    location_title = config.get("location_title")

    if account_id and location_id:
        # Configured target — skip discovery entirely.
        location = {"id": location_id, "title": location_title}
    else:
        # Auto-discover: first account -> first location.
        accounts = (
            _gb_call(session, workspace_id, "list_accounts", {}) or {}
        ).get("accounts") or []
        if not accounts:
            return 0
        account_id = accounts[0].get("id")
        if not account_id:
            return 0

        locations = (
            _gb_call(session, workspace_id, "list_locations",
                     {"account_id": account_id}) or {}
        ).get("locations") or []
        if not locations:
            return 0
        location = locations[0]
        location_id = location.get("id")
        if not location_id:
            return 0

    result = _gb_call(
        session, workspace_id, "list_reviews",
        {"account_id": account_id, "location_id": location_id,
         "max_results": REPUTATION_MAX_PER_TICK},
    )
    reviews = (result or {}).get("reviews") or []
    ids = [r.get("id") for r in reviews if r.get("id")]
    if not ids:
        return 0

    from sqlalchemy import bindparam
    seen_stmt = text(
        """
        SELECT payload ->> 'review_id' AS rid
          FROM commands
         WHERE workspace_id = :ws
           AND kind = :kind
           AND payload ->> 'source' = :source
           AND payload ->> 'review_id' IN :ids
        """
    ).bindparams(bindparam("ids", expanding=True))
    seen = {
        r[0] for r in session.execute(
            seen_stmt,
            {"ws": workspace_id, "kind": REPUTATION_KIND,
             "source": REPUTATION_SOURCE, "ids": ids},
        )
    }

    where = location.get("title") or "Google Business Profile"
    enqueued = 0
    for r in reviews:
        rid = r.get("id")
        if not rid or rid in seen:
            continue
        review = {
            # Field names the reputation assistant reads: text + rating +
            # author + source.
            "text": r.get("comment") or "",
            "rating": r.get("rating"),
            "author": r.get("reviewer"),
            "source": where,
            "review_id": rid,
            "created_at": r.get("create_time"),
        }
        cmd_id = str(uuid.uuid4())
        payload = {
            "source": REPUTATION_SOURCE,
            "review_id": rid,
            "review": review,
        }
        session.execute(
            text(
                """
                INSERT INTO commands (
                    id, workspace_id, agent_name, kind, payload, status,
                    approval_state, approved_at, created_at, expires_at,
                    dispatch_chain_id, dispatch_depth
                ) VALUES (
                    :id, :ws, :agent, :kind, CAST(:payload AS JSONB),
                    'pending', 'auto_approved', :now, :now, :expires,
                    :chain, 0
                )
                """
            ),
            {
                "id": cmd_id,
                "ws": workspace_id,
                "agent": REPUTATION_AGENT,
                "kind": REPUTATION_KIND,
                "payload": _json_dumps(payload),
                "now": now,
                "expires": now + _COMMAND_TTL,
                "chain": cmd_id,
            },
        )
        enqueued += 1

    if enqueued:
        logger.info(
            "feeder: enqueued %d review(s) for ws=%s", enqueued, workspace_id
        )
    return enqueued


def _has_recent_website_check(
    session: Session, workspace_id: str, now: datetime
) -> bool:
    """True if a feeder website.check was enqueued inside the cadence
    window. Keyed on the WEBSITE_SOURCE marker so an owner's manual
    "check now" (a different source) never blocks the scheduled sweep."""
    floor = now - WEBSITE_DEDUP_WINDOW
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
        {"ws": workspace_id, "agent": WEBSITE_AGENT, "kind": WEBSITE_KIND,
         "floor": floor, "source": WEBSITE_SOURCE},
    ).first()
    return found is not None


def enqueue_website_check_for_workspace(
    session: Session, workspace_id: str, now: datetime
) -> int:
    """Enqueue one website.check for the workspace's configured site, on a
    daily cadence. Returns 1 if enqueued, 0 if skipped.

    The target URL lives in the website_health feeder config (set in
    onboarding or the feeder settings UI). With no URL configured the feeder
    is a clean no-op — that's the pre-setup state, not an error. Dedup is a
    time window (one check per ~day), not per-item: this is a recurring
    sweep of one site, unlike the review/inbox feeders that fan out per new
    external item. Does not commit; the caller owns the transaction.
    """
    url = normalize_website_url(
        get_feeder_config(session, workspace_id, FEEDER_WEBSITE_HEALTH).get("url")
    )
    if not url:
        return 0
    if _has_recent_website_check(session, workspace_id, now):
        return 0

    cmd_id = str(uuid.uuid4())
    payload = {"source": WEBSITE_SOURCE, "url": url}
    session.execute(
        text(
            """
            INSERT INTO commands (
                id, workspace_id, agent_name, kind, payload, status,
                approval_state, approved_at, created_at, expires_at,
                dispatch_chain_id, dispatch_depth
            ) VALUES (
                :id, :ws, :agent, :kind, CAST(:payload AS JSONB),
                'pending', 'auto_approved', :now, :now, :expires,
                :chain, 0
            )
            """
        ),
        {
            "id": cmd_id,
            "ws": workspace_id,
            "agent": WEBSITE_AGENT,
            "kind": WEBSITE_KIND,
            "payload": _json_dumps(payload),
            "now": now,
            "expires": now + _COMMAND_TTL,
            "chain": cmd_id,
        },
    )
    logger.info(
        "feeder: enqueued website check ws=%s url=%s cmd=%s",
        workspace_id, url, cmd_id,
    )
    return 1


def _has_recent_seo_audit(
    session: Session, workspace_id: str, now: datetime
) -> bool:
    """True if a feeder seo.audit was enqueued inside the cadence window."""
    floor = now - SEO_DEDUP_WINDOW
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
        {"ws": workspace_id, "agent": SEO_AGENT, "kind": SEO_KIND,
         "floor": floor, "source": SEO_SOURCE},
    ).first()
    return found is not None


def enqueue_seo_audit_for_workspace(
    session: Session, workspace_id: str, now: datetime
) -> int:
    """Enqueue one seo.audit for the workspace's configured site, on a daily
    cadence. Returns 1 if enqueued, 0 if skipped. Same periodic-sweep shape
    as the website-health feeder: URL from the seo_audit feeder config,
    time-window dedup, no-op until a URL is set. Does not commit."""
    url = normalize_website_url(
        get_feeder_config(session, workspace_id, FEEDER_SEO_AUDIT).get("url")
    )
    if not url:
        return 0
    if _has_recent_seo_audit(session, workspace_id, now):
        return 0

    cmd_id = str(uuid.uuid4())
    payload = {"source": SEO_SOURCE, "url": url}
    session.execute(
        text(
            """
            INSERT INTO commands (
                id, workspace_id, agent_name, kind, payload, status,
                approval_state, approved_at, created_at, expires_at,
                dispatch_chain_id, dispatch_depth
            ) VALUES (
                :id, :ws, :agent, :kind, CAST(:payload AS JSONB),
                'pending', 'auto_approved', :now, :now, :expires,
                :chain, 0
            )
            """
        ),
        {
            "id": cmd_id,
            "ws": workspace_id,
            "agent": SEO_AGENT,
            "kind": SEO_KIND,
            "payload": _json_dumps(payload),
            "now": now,
            "expires": now + _COMMAND_TTL,
            "chain": cmd_id,
        },
    )
    logger.info(
        "feeder: enqueued seo audit ws=%s url=%s cmd=%s",
        workspace_id, url, cmd_id,
    )
    return 1


def _has_recent_feeder_command(
    session: Session,
    workspace_id: str,
    now: datetime,
    *,
    source: str,
    window: timedelta = DEDUP_WINDOW,
) -> bool:
    """True if a feeder command with this ``source`` was enqueued inside
    ``window``.

    Keyed on the payload's ``source`` marker so each feeder dedups against
    its own kind only: a human- or dashboard-requested bi.summarize never
    blocks the weekly digest, and the digest never blocks a cost alert
    (both are bi.summarize commands, distinguished only by source). Uses a
    JSONB ``->>`` text match so the check runs without loading rows.
    """
    floor = now - window
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
            "source": source,
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


def gather_recent_activity(
    session: Session,
    workspace_id: str,
    now: datetime,
    *,
    period_days: int = PERIOD_DAYS,
) -> dict[str, Any]:
    """Roll the workspace's last `period_days` of events into the data dict
    the BI assistant analyzes. Shared by the weekly digest feeder and the
    'ask your team' endpoint, so a question is answered against the same
    activity rollup the digest summarizes."""
    events = _recent_events(session, workspace_id, now, period_days)
    return build_digest_payload(
        events, period_days=period_days, now_iso=now.isoformat()
    )


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
    if not force and _has_recent_feeder_command(
        session, workspace_id, now, source=DIGEST_SOURCE
    ):
        return None

    data = gather_recent_activity(
        session, workspace_id, now, period_days=period_days
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
    # BI-fed feeders (digest + cost alert) over workspaces running the BI
    # assistant. Each feeder is independent + best-effort + gated on the
    # owner's setting: one feeder (or workspace) failing never blocks the
    # rest.
    for workspace_id in _workspaces_running_agent(session, DIGEST_AGENT):
        if is_feeder_enabled(session, workspace_id, FEEDER_WEEKLY_DIGEST):
            try:
                if enqueue_digest_for_workspace(session, workspace_id, now):
                    enqueued += 1
            except Exception:  # noqa: BLE001 — best-effort, keep going
                logger.exception(
                    "feeder: failed to enqueue digest for ws=%s", workspace_id
                )
        if is_feeder_enabled(session, workspace_id, FEEDER_COST_SPIKE):
            try:
                if enqueue_cost_alert_for_workspace(session, workspace_id, now):
                    enqueued += 1
            except Exception:  # noqa: BLE001 — best-effort, keep going
                logger.exception(
                    "feeder: failed to enqueue cost alert for ws=%s",
                    workspace_id,
                )

    # Inbox feeder (default OFF) over workspaces running the Inbox
    # assistant. Only polls Gmail where the owner has opted in.
    for workspace_id in _workspaces_running_agent(session, INBOX_AGENT):
        if is_feeder_enabled(session, workspace_id, FEEDER_INBOX_GMAIL):
            try:
                enqueued += enqueue_inbox_items_for_workspace(
                    session, workspace_id, now
                )
            except Exception:  # noqa: BLE001 — best-effort, keep going
                logger.exception(
                    "feeder: failed to enqueue inbox items for ws=%s",
                    workspace_id,
                )

    # Reputation feeder (default OFF) over workspaces running the
    # Reputation assistant. Only polls Google Business Profile where the
    # owner has opted in.
    for workspace_id in _workspaces_running_agent(session, REPUTATION_AGENT):
        if is_feeder_enabled(session, workspace_id, FEEDER_REPUTATION_REVIEWS):
            try:
                enqueued += enqueue_reputation_reviews_for_workspace(
                    session, workspace_id, now
                )
            except Exception:  # noqa: BLE001 — best-effort, keep going
                logger.exception(
                    "feeder: failed to enqueue reviews for ws=%s",
                    workspace_id,
                )

    # Website feeder (default ON, no-op until a URL is configured) over
    # workspaces running the Website assistant. One health check per day
    # for the owner's configured site.
    for workspace_id in _workspaces_running_agent(session, WEBSITE_AGENT):
        if is_feeder_enabled(session, workspace_id, FEEDER_WEBSITE_HEALTH):
            try:
                enqueued += enqueue_website_check_for_workspace(
                    session, workspace_id, now
                )
            except Exception:  # noqa: BLE001 — best-effort, keep going
                logger.exception(
                    "feeder: failed to enqueue website check for ws=%s",
                    workspace_id,
                )

    # SEO feeder (default ON, no-op until a URL is configured) over
    # workspaces running the SEO assistant. One on-page audit per day for
    # the owner's configured site — this is the "constantly working" part.
    for workspace_id in _workspaces_running_agent(session, SEO_AGENT):
        if is_feeder_enabled(session, workspace_id, FEEDER_SEO_AUDIT):
            try:
                enqueued += enqueue_seo_audit_for_workspace(
                    session, workspace_id, now
                )
            except Exception:  # noqa: BLE001 — best-effort, keep going
                logger.exception(
                    "feeder: failed to enqueue seo audit for ws=%s",
                    workspace_id,
                )
    return enqueued


def _json_dumps(value: Any) -> str:
    import json

    return json.dumps(value)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
