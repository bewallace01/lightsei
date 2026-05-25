"""Phase 24.3: tests for the deploy → planner-values write contract.

The dashboard's team-from-readme deploy step (after Phase 24.3)
sends the planner's m.sensitivity_hint + m.capabilities through
the existing PATCH /agents/{name} + PATCH /agents/{name}/capabilities
endpoints right after upload_deployment lays down the Agent row
via ensure_agent (which leaves sensitivity_level at the server
default + capabilities at []).

These tests exercise the same calls the dashboard now makes, end
to end, to lock the contract: a fresh deploy that flows through
the planner-values PATCHes results in an Agent row carrying the
planner's structured zone + capability allow-list — not the
server defaults the bot would otherwise ship with.
"""
from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone

from db import session_scope
from models import Agent
from tests.conftest import auth_headers


def _make_zip_bundle() -> bytes:
    """Minimal zip the worker would accept for a deploy. The deploy
    handler reads the bundle but doesn't execute it — these tests
    don't need real Python code, just non-empty bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("bot.py", "import lightsei\nlightsei.init()\n")
        zf.writestr("requirements.txt", "lightsei>=0.1.0\n")
    return buf.getvalue()


def _upload_deploy(client, *, api_key: str, agent_name: str):
    return client.post(
        "/workspaces/me/deployments",
        headers=auth_headers(api_key),
        files={"bundle": ("bundle.zip", _make_zip_bundle(), "application/zip")},
        data={"agent_name": agent_name},
    )


def test_deploy_then_planner_patches_lands_pii_zone_and_narrow_caps(client, alice):
    """The JYNI-flavored case: a CRM-bound bot ships as pii with a
    one-entry capability list, not the server defaults."""
    api_key = alice["api_key"]["plaintext"]

    # 1. Upload bundle → Agent row exists with server defaults.
    r = _upload_deploy(client, api_key=api_key, agent_name="vega")
    assert r.status_code == 200, r.text

    with session_scope() as s:
        row = s.execute(
            __import__("sqlalchemy").select(Agent).where(Agent.name == "vega")
        ).scalars().first()
        # Defaults from ensure_agent.
        assert row.sensitivity_level == "internal"
        assert row.capabilities == []

    # 2. Dashboard sends the planner's m.sensitivity_hint via PATCH.
    r = client.patch(
        "/agents/vega",
        headers=auth_headers(api_key),
        json={"sensitivity_level": "pii"},
    )
    assert r.status_code == 200, r.text

    # 3. Dashboard sends the planner's m.capabilities via PATCH.
    r = client.patch(
        "/agents/vega/capabilities",
        headers=auth_headers(api_key),
        json={"capabilities": ["connector:jyni_crm"]},
    )
    assert r.status_code == 200, r.text

    # 4. Agent row now carries the planner's structured fields.
    with session_scope() as s:
        row = s.execute(
            __import__("sqlalchemy").select(Agent).where(Agent.name == "vega")
        ).scalars().first()
        assert row.sensitivity_level == "pii"
        assert row.capabilities == ["connector:jyni_crm"]


def test_deploy_then_planner_patches_public_research_bot(client, alice):
    """Symmetric case: a web research bot ships as public with
    `internet`, not as the role-based default."""
    api_key = alice["api_key"]["plaintext"]

    r = _upload_deploy(client, api_key=api_key, agent_name="atlas")
    assert r.status_code == 200

    client.patch(
        "/agents/atlas",
        headers=auth_headers(api_key),
        json={"sensitivity_level": "public"},
    )
    client.patch(
        "/agents/atlas/capabilities",
        headers=auth_headers(api_key),
        json={"capabilities": ["internet", "send_command"]},
    )

    with session_scope() as s:
        row = s.execute(
            __import__("sqlalchemy").select(Agent).where(Agent.name == "atlas")
        ).scalars().first()
        assert row.sensitivity_level == "public"
        assert row.capabilities == ["internet", "send_command"]


def test_deploy_then_empty_capabilities_keeps_operator_only(client, alice):
    """A planner that emitted m.capabilities=[] (operator-only bot)
    ships with an empty allow-list. Phase 16's SDK gate then refuses
    every gated op until the operator grants caps via /agents/{name}/
    capabilities — which is the explicit 'no surprises' contract from
    24.1's narrow-allow-list rule."""
    api_key = alice["api_key"]["plaintext"]

    r = _upload_deploy(client, api_key=api_key, agent_name="orion")
    assert r.status_code == 200

    client.patch(
        "/agents/orion",
        headers=auth_headers(api_key),
        json={"sensitivity_level": "internal"},
    )
    client.patch(
        "/agents/orion/capabilities",
        headers=auth_headers(api_key),
        json={"capabilities": []},
    )

    with session_scope() as s:
        row = s.execute(
            __import__("sqlalchemy").select(Agent).where(Agent.name == "orion")
        ).scalars().first()
        assert row.sensitivity_level == "internal"
        assert row.capabilities == []
