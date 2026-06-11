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
        "feeders": [],
        "connector": None,
    },
]
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
            }
            for g in GOALS
        ],
        "recommendations": {
            i["key"]: recommended_goals(i["key"]) for i in INDUSTRIES
        },
    }


def build_provisioning_plan(
    industry: Optional[str], goal_keys: list[str]
) -> dict[str, Any]:
    """Map answers to a provisioning plan. Pure: no DB.

    Unknown goal keys are dropped (not an error — a stale client shouldn't
    fail the whole submit). Order + dedup are stable so the plan reads the
    same way every time. Returns the assistants to create, feeders to
    enable, and connectors the owner still needs to connect.
    """
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

    return {
        "industry": industry,
        "goals": [g["key"] for g in selected],
        "assistants": agents,
        "feeders": feeders,
        "connectors_needed": [
            {"type": c, "label": _CONNECTOR_LABELS.get(c, c)}
            for c in connectors
        ],
    }


def apply_provisioning_plan(
    session: Session,
    workspace_id: str,
    plan: dict[str, Any],
    now: datetime,
) -> None:
    """Provision the plan: create assistant rows, enable feeders, store the
    profile on the workspace. Idempotent (ensure_agent + feeder upsert +
    profile overwrite). Does not commit.
    """
    import feeder
    from db import ensure_agent

    for agent_name in plan.get("assistants", []):
        ensure_agent(session, workspace_id, agent_name, now)

    for goal_key in plan.get("goals", []):
        goal = _GOAL_BY_KEY.get(goal_key)
        if not goal or not goal.get("connector"):
            continue
        _grant_agent_capabilities(
            session,
            workspace_id,
            goal["agent"],
            [f"connector:{goal['connector']}"],
            now,
        )

    for feeder_kind in plan.get("feeders", []):
        feeder.set_feeder_enabled(session, workspace_id, feeder_kind, True, now)

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


def _grant_agent_capabilities(
    session: Session,
    workspace_id: str,
    agent_name: str,
    capabilities: list[str],
    now: datetime,
) -> None:
    """Merge required connector powers into an onboarding-created assistant."""
    if not capabilities:
        return
    row = session.execute(
        text(
            """
            SELECT capabilities
              FROM agents
             WHERE workspace_id = :ws AND name = :name
             FOR UPDATE
            """
        ),
        {"ws": workspace_id, "name": agent_name},
    ).first()
    if row is None:
        return

    current = list(row[0] or [])
    changed = False
    for capability in capabilities:
        if capability in current:
            continue
        current.append(capability)
        changed = True

    if not changed:
        return
    session.execute(
        text(
            """
            UPDATE agents
               SET capabilities = CAST(:capabilities AS JSONB),
                   updated_at = :now
             WHERE workspace_id = :ws AND name = :name
            """
        ),
        {
            "ws": workspace_id,
            "name": agent_name,
            "capabilities": _json_dumps(current),
            "now": now,
        },
    )


def _json_dumps(value: Any) -> str:
    import json

    return json.dumps(value)
