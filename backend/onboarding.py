"""Phase 33.1: business-shaped onboarding.

The vision's "sign up -> answer a few questions -> working AI team", for a
non-technical owner. Replaces the developer-shaped README flow: instead of
describing a codebase, the owner picks their industry and what they want
help with, and we provision the matching assistants + turn on the right
feeders.

This module is the pure core: the catalog (industries + goals) and the
mapping from answers to a provisioning plan. `apply_provisioning_plan`
(the one DB-touching function) creates the assistant rows + enables the
feeders + stores the profile. No deployment happens here: an assistant
row is its identity; the worker brings it online separately, and the
existing surfaces already handle "provisioned but not deployed yet"
gracefully. Onboarding surfaces that as a next step rather than hiding it.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session


INDUSTRIES = [
    {"key": "restaurant", "label": "Restaurant or cafe"},
    {"key": "home_services", "label": "Home services"},
    {"key": "retail", "label": "Retail or e-commerce"},
    {"key": "professional", "label": "Professional services"},
    {"key": "other", "label": "Something else"},
]
_INDUSTRY_KEYS = {i["key"] for i in INDUSTRIES}

# Each goal maps to the assistant that does the job, the feeders it should
# turn on, and the connector (if any) the owner must connect for it to run
# live. `agent` matches the persona names the worker deploys + the feeders
# target (see feeder.py / agents/<name>).
GOALS = [
    {
        "key": "email",
        "label": "Answer customer emails",
        "agent": "inbox",
        "feeders": ["inbox_gmail"],
        "connector": "gmail",
    },
    {
        "key": "reviews",
        "label": "Watch our online reviews",
        "agent": "reputation",
        "feeders": ["reputation_reviews"],
        "connector": "google_business",
    },
    {
        "key": "marketing",
        "label": "Write marketing posts and copy",
        "agent": "marketing",
        "feeders": [],
        "connector": None,
    },
    {
        "key": "summary",
        "label": "Get a weekly business summary",
        "agent": "bi",
        "feeders": ["weekly_digest", "cost_spike"],
        "connector": None,
    },
    {
        "key": "leads",
        "label": "Capture and qualify leads",
        "agent": "lead",
        "feeders": [],
        "connector": None,
    },
    {
        "key": "website",
        "label": "Monitor our website",
        "agent": "website",
        "feeders": ["website_health"],
        "connector": None,
        # Needs a free-text target (the site URL) rather than a connector;
        # the wizard collects it when this goal is checked.
        "needs_url": True,
    },
    {
        "key": "seo",
        "label": "Improve our search ranking (SEO)",
        "agent": "seo",
        "feeders": ["seo_audit"],
        "connector": None,
        # Shares the same site URL as the website goal.
        "needs_url": True,
    },
]
# Goals whose feeder targets the owner's own site URL (collected once in the
# wizard, applied to each selected goal's url-target feeder).
_URL_GOALS = {"website", "seo"}
_URL_GOAL_FEEDER = {"website": "website_health", "seo": "seo_audit"}
_GOAL_BY_KEY = {g["key"]: g for g in GOALS}

# Industry -> the goals we pre-check. A starting point the owner edits, not
# a constraint: any goal is selectable regardless of industry.
_INDUSTRY_DEFAULTS = {
    "restaurant": ["reviews", "email", "summary"],
    "home_services": ["leads", "reviews", "summary"],
    "retail": ["email", "marketing", "summary"],
    "professional": ["email", "leads", "summary"],
    "other": ["summary"],
}

# Connector type -> friendly label, for the "connect these next" prompt.
_CONNECTOR_LABELS = {
    "gmail": "Gmail",
    "google_business": "Google Business Profile",
}


def recommended_goals(industry: Optional[str]) -> list[str]:
    """The pre-checked goals for an industry (empty for unknown)."""
    return list(_INDUSTRY_DEFAULTS.get(industry or "", []))


def catalog() -> dict[str, Any]:
    """Industries + goals + per-industry recommendations for the wizard."""
    return {
        "industries": INDUSTRIES,
        "goals": [
            {
                "key": g["key"],
                "label": g["label"],
                "assistant": g["agent"],
                "connector": g["connector"],
                "needs_url": bool(g.get("needs_url")),
            }
            for g in GOALS
        ],
        "recommendations": {
            i["key"]: recommended_goals(i["key"]) for i in INDUSTRIES
        },
    }


def build_provisioning_plan(
    industry: Optional[str],
    goal_keys: list[str],
    website_url: Optional[str] = None,
) -> dict[str, Any]:
    """Map answers to a provisioning plan. Pure: no DB.

    Unknown goal keys are dropped (not an error — a stale client shouldn't
    fail the whole submit). Order + dedup are stable so the plan reads the
    same way every time. Returns the assistants to create, feeders to
    enable, and connectors the owner still needs to connect.

    `website_url` is the site address the owner typed for the "Monitor our
    website" goal. It's normalized here and carried on the plan only when
    that goal is selected and the URL parses; apply_provisioning_plan
    stores it as the website feeder's target.
    """
    import feeder
    # Dedup keys (stable order) before mapping, so a stale client sending
    # duplicates doesn't store dup goals.
    seen_keys: set[str] = set()
    ordered_keys: list[str] = []
    for k in goal_keys:
        if k in _GOAL_BY_KEY and k not in seen_keys:
            seen_keys.add(k)
            ordered_keys.append(k)
    selected = [_GOAL_BY_KEY[k] for k in ordered_keys]

    agents: list[str] = []
    feeders: list[str] = []
    connectors: list[str] = []
    for g in selected:
        if g["agent"] not in agents:
            agents.append(g["agent"])
        for f in g["feeders"]:
            if f not in feeders:
                feeders.append(f)
        c = g["connector"]
        if c and c not in connectors:
            connectors.append(c)

    # Carry the site URL only when a URL goal (website / seo) is in play and
    # it normalizes to a real URL — otherwise the plan stays clean (no target
    # for an unselected goal, no garbage from a typo).
    normalized_url = (
        feeder.normalize_website_url(website_url)
        if _URL_GOALS & set(ordered_keys)
        else None
    )

    return {
        "industry": industry,
        "goals": [g["key"] for g in selected],
        "assistants": agents,
        "feeders": feeders,
        "website_url": normalized_url,
        "connectors_needed": [
            {"type": c, "label": _CONNECTOR_LABELS.get(c, c)}
            for c in connectors
        ],
    }


def _grant_connector_capability(
    session: Session, workspace_id: str, agent_name: str, connector: str,
    now: datetime,
) -> None:
    """Grant an assistant the `connector:<type>` capability so the feeder's
    gated connector call (dispatched as that assistant) passes the Phase 16
    capability guardrail. Without it, a user who connects Gmail / reviews
    gets a feeder that's silently refused and never runs. Idempotent: a
    capability already present is left as-is. Does not commit.
    """
    import json

    capability = f"connector:{connector}"
    row = session.execute(
        text("SELECT capabilities FROM agents "
             "WHERE workspace_id = :w AND name = :n"),
        {"w": workspace_id, "n": agent_name},
    ).first()
    if row is None:
        return
    caps = list(row[0] or [])
    if capability in caps:
        return
    caps.append(capability)
    session.execute(
        text("UPDATE agents SET capabilities = CAST(:caps AS JSONB), "
             "updated_at = :now WHERE workspace_id = :w AND name = :n"),
        {"caps": json.dumps(caps), "now": now, "w": workspace_id, "n": agent_name},
    )


def apply_provisioning_plan(
    session: Session,
    workspace_id: str,
    plan: dict[str, Any],
    now: datetime,
) -> None:
    """Provision the plan: create assistant rows, grant each assistant the
    connector capability its goal needs, enable feeders, store the profile
    on the workspace. Idempotent (ensure_agent + capability dedup + feeder
    upsert + profile overwrite). Does not commit.
    """
    import feeder
    from db import ensure_agent

    for agent_name in plan.get("assistants", []):
        ensure_agent(session, workspace_id, agent_name, now)

    # Grant each goal's connector capability to its assistant (after the
    # rows exist). The "email" goal -> inbox needs connector:gmail, "reviews"
    # -> reputation needs connector:google_business, etc.
    for goal_key in plan.get("goals", []):
        g = _GOAL_BY_KEY.get(goal_key)
        if g and g.get("connector"):
            _grant_connector_capability(
                session, workspace_id, g["agent"], g["connector"], now
            )

    for feeder_kind in plan.get("feeders", []):
        feeder.set_feeder_enabled(session, workspace_id, feeder_kind, True, now)

    # Store the site URL on each selected url-target feeder (website_health
    # and/or seo_audit). Done after enabling so the config upsert just touches
    # the url; with no URL these feeders stay a no-op until one is set.
    site_url = plan.get("website_url")
    if site_url:
        for goal_key in plan.get("goals", []):
            feeder_kind = _URL_GOAL_FEEDER.get(goal_key)
            if feeder_kind:
                feeder.set_feeder_config(
                    session, workspace_id, feeder_kind, {"url": site_url}, now,
                )

    profile = {
        "industry": plan.get("industry"),
        "goals": plan.get("goals", []),
        "completed_at": now.isoformat(),
    }
    session.execute(
        text(
            """
            UPDATE workspaces
               SET onboarding_profile = CAST(:profile AS JSONB)
             WHERE id = :ws
            """
        ),
        {"ws": workspace_id, "profile": _json_dumps(profile)},
    )


def get_profile(session: Session, workspace_id: str) -> Optional[dict[str, Any]]:
    """The stored onboarding profile, or None if onboarding isn't done."""
    row = session.execute(
        text("SELECT onboarding_profile FROM workspaces WHERE id = :ws"),
        {"ws": workspace_id},
    ).first()
    if row is None or not row[0]:
        return None
    return dict(row[0])


def _json_dumps(value: Any) -> str:
    import json

    return json.dumps(value)
