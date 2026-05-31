"""Phase 30.3.b: tests for the Polaris router.

Surfaces:

1. Empty workspace short-circuits with no Anthropic call.
2. Router picks one agent → RouteDecision carries that pick.
3. Router picks multiple agents → all survive.
4. Router picks an unknown name → that pick is silently dropped
   (defends the dispatch step from hallucinated agents the claim loop
   would never satisfy).
5. Router returns no agents → RouteDecision.agents == [] (legal).
6. Missing ANTHROPIC_API_KEY → RouterError.
7. Response with no tool_use block → RouterError.
8. system_prompt + ROUTE_TEAM_TOOL shape spot-checks.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

import secrets_crypto
import team_router
from db import session_scope
from models import Agent, Workspace, WorkspaceSecret


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_workspace(s) -> str:
    wid = str(uuid.uuid4())
    s.add(Workspace(
        id=wid,
        name=f"team-router-{wid[:8]}",
        created_at=_utcnow(),
    ))
    s.flush()
    return wid


def _make_agent(
    s, workspace_id: str, name: str, *,
    role: str = "specialist",
    description: str | None = None,
    capabilities: list[str] | None = None,
) -> None:
    s.add(Agent(
        workspace_id=workspace_id,
        name=name,
        role=role,
        description=description,
        capabilities=capabilities or [],
        created_at=_utcnow(),
        updated_at=_utcnow(),
    ))


def _set_anthropic_secret(s, workspace_id: str) -> None:
    s.add(WorkspaceSecret(
        workspace_id=workspace_id,
        name="ANTHROPIC_API_KEY",
        encrypted_value=secrets_crypto.encrypt("sk-ant-fake-for-tests"),
        created_at=_utcnow(),
        updated_at=_utcnow(),
    ))


def _fake_tool_use(*, summary: str, agents: list[dict[str, str]]):
    """Build a stand-in for Anthropic's SDK response with one
    tool_use content block carrying the router payload."""
    block = SimpleNamespace(
        type="tool_use",
        name="route_team_message",
        input={"summary": summary, "agents": agents},
    )
    return SimpleNamespace(content=[block])


def _fake_no_tool_use():
    text_block = SimpleNamespace(type="text", text="oops, no tool")
    return SimpleNamespace(content=[text_block])


def _make_factory(response: Any):
    """Returns an anthropic_factory that produces a client whose
    messages.create returns the canned response."""
    client = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **kw: response),
    )
    return lambda api_key: client


# ---------- Empty workspace ---------- #


def test_empty_workspace_returns_empty_decision_without_calling_llm():
    """Polaris should never reach Anthropic if there's nobody to
    route to. Factory is rigged to blow up so a regression is loud."""
    def boom_factory(api_key):
        raise AssertionError("Anthropic should not be called")

    with session_scope() as s:
        wid = _make_workspace(s)
        # No agents, no secret needed — short-circuit happens first.

    with session_scope() as s:
        decision = team_router.route_team_message(
            s, wid, "anyone here?", anthropic_factory=boom_factory,
        )
    assert decision.agents == []
    assert "no bots" in decision.summary.lower()


# ---------- Happy paths ---------- #


def test_routes_to_single_agent():
    with session_scope() as s:
        wid = _make_workspace(s)
        _make_agent(s, wid, "argus", description="scans pushes for secrets")
        _make_agent(s, wid, "hermes", description="posts to slack")
        _set_anthropic_secret(s, wid)

    resp = _fake_tool_use(
        summary="Argus handles this scan request.",
        agents=[{"name": "argus", "reason": "secret scan"}],
    )
    with session_scope() as s:
        decision = team_router.route_team_message(
            s, wid, "scan the last commit for hardcoded API keys",
            anthropic_factory=_make_factory(resp),
        )
    assert [p.name for p in decision.agents] == ["argus"]
    assert decision.agents[0].reason == "secret scan"
    assert decision.summary == "Argus handles this scan request."


def test_routes_to_multiple_agents():
    with session_scope() as s:
        wid = _make_workspace(s)
        _make_agent(s, wid, "argus", description="scans pushes for secrets")
        _make_agent(s, wid, "hermes", description="posts to slack")
        _make_agent(s, wid, "vega", description="watches metrics")
        _set_anthropic_secret(s, wid)

    resp = _fake_tool_use(
        summary="Argus + hermes will handle this.",
        agents=[
            {"name": "argus", "reason": "scan"},
            {"name": "hermes", "reason": "notify"},
        ],
    )
    with session_scope() as s:
        decision = team_router.route_team_message(
            s, wid, "scan + post results to ops",
            anthropic_factory=_make_factory(resp),
        )
    assert [p.name for p in decision.agents] == ["argus", "hermes"]


def test_routes_to_zero_agents_is_legal():
    """The router is allowed to decide nobody should respond
    (off-topic message, etc.) and that flows through cleanly."""
    with session_scope() as s:
        wid = _make_workspace(s)
        _make_agent(s, wid, "argus")
        _set_anthropic_secret(s, wid)

    resp = _fake_tool_use(
        summary="Off-topic; no bot should answer.",
        agents=[],
    )
    with session_scope() as s:
        decision = team_router.route_team_message(
            s, wid, "what's the weather?",
            anthropic_factory=_make_factory(resp),
        )
    assert decision.agents == []
    assert "off-topic" in decision.summary.lower()


# ---------- Hallucinated names ---------- #


def test_unknown_agent_names_are_dropped():
    """If the router picks an agent that doesn't exist in the
    workspace (model hallucination, stale agent list), drop it
    silently rather than create a pending row no agent will claim."""
    with session_scope() as s:
        wid = _make_workspace(s)
        _make_agent(s, wid, "argus")
        _set_anthropic_secret(s, wid)

    resp = _fake_tool_use(
        summary="Both should weigh in.",
        agents=[
            {"name": "argus", "reason": "real"},
            {"name": "phantom-agent", "reason": "imagined"},
        ],
    )
    with session_scope() as s:
        decision = team_router.route_team_message(
            s, wid, "anyone?",
            anthropic_factory=_make_factory(resp),
        )
    assert [p.name for p in decision.agents] == ["argus"]


def test_duplicate_picks_are_collapsed():
    with session_scope() as s:
        wid = _make_workspace(s)
        _make_agent(s, wid, "argus")
        _set_anthropic_secret(s, wid)

    resp = _fake_tool_use(
        summary="argus once.",
        agents=[
            {"name": "argus", "reason": "first"},
            {"name": "argus", "reason": "second"},
        ],
    )
    with session_scope() as s:
        decision = team_router.route_team_message(
            s, wid, "scan",
            anthropic_factory=_make_factory(resp),
        )
    assert [p.name for p in decision.agents] == ["argus"]
    assert decision.agents[0].reason == "first"  # first-write-wins


# ---------- Error paths ---------- #


def test_missing_api_key_raises_router_error():
    with session_scope() as s:
        wid = _make_workspace(s)
        _make_agent(s, wid, "argus")
        # No WorkspaceSecret on purpose.

    with session_scope() as s:
        with pytest.raises(team_router.RouterError) as ei:
            team_router.route_team_message(
                s, wid, "hi",
                anthropic_factory=_make_factory(_fake_tool_use(
                    summary="x", agents=[],
                )),
            )
    assert "ANTHROPIC_API_KEY" in str(ei.value)


def test_no_tool_use_block_raises_router_error():
    """If the model ignores tool_choice and only emits text,
    surface a clear error so the caller falls back gracefully
    (router row content = error text, no assistant rows)."""
    with session_scope() as s:
        wid = _make_workspace(s)
        _make_agent(s, wid, "argus")
        _set_anthropic_secret(s, wid)

    with session_scope() as s:
        with pytest.raises(team_router.RouterError) as ei:
            team_router.route_team_message(
                s, wid, "hi",
                anthropic_factory=_make_factory(_fake_no_tool_use()),
            )
    assert "tool_use" in str(ei.value)


# ---------- Shape spot-checks ---------- #


def test_tool_schema_required_fields():
    """Spot-check the schema the LLM is forced into so a refactor
    doesn't quietly drop summary/agents."""
    schema = team_router.ROUTE_TEAM_TOOL["input_schema"]
    assert schema["required"] == ["summary", "agents"]
    item = schema["properties"]["agents"]["items"]
    assert item["required"] == ["name", "reason"]


def test_system_prompt_lists_every_agent():
    """Polaris's system prompt must show each agent so it can pick
    from a full menu. Filtering happens INSIDE the prompt build,
    not at runtime — regressions here would silently shrink the
    candidate set."""
    agents = [
        {"name": "argus", "role": "specialist", "description": "scan", "capabilities": []},
        {"name": "hermes", "role": "notifier", "description": "slack post", "capabilities": ["slack:respond"]},
    ]
    sp = team_router.build_system_prompt(agents)
    assert "argus" in sp
    assert "hermes" in sp
    assert "slack:respond" in sp


def test_routed_agents_jsonb_serializer_is_db_ready():
    """The team_messages.routed_agents column expects a dict whose
    'agents' key holds [{name, reason}]. RouteDecision must
    serialize directly into that shape."""
    decision = team_router.RouteDecision(
        summary="ignored here",
        agents=[
            team_router.AgentPick("argus", "scan"),
            team_router.AgentPick("hermes", "notify"),
        ],
    )
    assert decision.as_routed_agents_json() == {
        "agents": [
            {"name": "argus", "reason": "scan"},
            {"name": "hermes", "reason": "notify"},
        ],
    }


def test_lightsei_system_agents_are_filtered_out():
    """`lightsei.*` agents are internal plumbing and must never
    be routed to."""
    with session_scope() as s:
        wid = _make_workspace(s)
        _make_agent(s, wid, "argus")
        _make_agent(s, wid, "lightsei.system")

    with session_scope() as s:
        routable = team_router.list_routable_agents(s, wid)
    assert {a["name"] for a in routable} == {"argus"}
