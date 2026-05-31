"""Phase 30.3.b: Polaris router for the team channel.

Given a workspace + an operator message, decide which subset of the
workspace's bots should respond. The router output drives the team
channel's dispatch step: one pending assistant row is inserted per
picked agent, and each agent's existing claim loop fills its own row.

Pure module: no FastAPI, no DB writes. Reads the workspace's agents +
ANTHROPIC_API_KEY, calls Anthropic with a forced tool_choice so the
output shape is guaranteed, validates the picks against the actual
agent set, returns a RouteDecision the caller serializes into the
team_messages "router" row (content = summary, routed_agents = picks
as JSON).

Anthropic call goes through `anthropic_factory` for test injection;
tests pass a fake client that returns a canned tool_use block.

Surfaces these failure modes the caller must handle:

  RouterError("ANTHROPIC_API_KEY missing for this workspace.")
  RouterError("failed to decrypt ANTHROPIC_API_KEY")
  RouterError("router returned no tool_use block")
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

import secrets_crypto
from models import Agent, WorkspaceSecret


class RouterError(Exception):
    """Raised when the router can't produce a usable decision."""


@dataclass
class AgentPick:
    name: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "reason": self.reason}


@dataclass
class RouteDecision:
    summary: str                  # one-line operator-visible explanation
    agents: list[AgentPick]       # may be empty (router decided nobody)

    def as_routed_agents_json(self) -> dict[str, Any]:
        """Serialize to the team_messages.routed_agents JSONB shape."""
        return {"agents": [p.as_dict() for p in self.agents]}


ROUTE_TEAM_TOOL: dict[str, Any] = {
    "name": "route_team_message",
    "description": (
        "Decide which of the workspace's bots should respond to this "
        "team message. Pick the smallest subset that makes sense; an "
        "empty list is valid if no bot is appropriate."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "One short sentence shown to the operator in the "
                    "channel explaining the routing decision."
                ),
            },
            "agents": {
                "type": "array",
                "description": "Picked agents, with a one-line reason each.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["name", "reason"],
                },
            },
        },
        "required": ["summary", "agents"],
    },
}


def list_routable_agents(
    session: Session, workspace_id: str,
) -> list[dict[str, Any]]:
    """Returns the agents Polaris is allowed to route to.

    Filters out `lightsei.*` system agents (these are internal to the
    platform + never participate in team chat).
    """
    rows = session.execute(
        select(Agent)
        .where(Agent.workspace_id == workspace_id)
        .order_by(Agent.name)
    ).scalars().all()
    out: list[dict[str, Any]] = []
    for a in rows:
        if a.name.startswith("lightsei."):
            continue
        out.append({
            "name": a.name,
            "role": a.role or "",
            "description": (a.description or "").strip(),
            "capabilities": list(a.capabilities or []),
        })
    return out


def build_system_prompt(agents: list[dict[str, Any]]) -> str:
    lines = [
        "You are Polaris, the team router for a Lightsei workspace.",
        "An operator just posted a message to the whole team. Decide "
        "which subset of the workspace's bots should respond. Skip "
        "bots whose role doesn't match. Keep the response set small "
        "and purposeful: an empty list is valid if no bot is "
        "appropriate.",
        "",
        "Available agents in this workspace:",
    ]
    for a in agents:
        cap = (
            ", ".join(a["capabilities"])
            if a["capabilities"] else "no extra capabilities"
        )
        desc = a["description"] or "(no description)"
        role = a["role"] or "specialist"
        lines.append(f"  - {a['name']} ({role}): {desc} [{cap}]")
    lines += [
        "",
        "Call the route_team_message tool with a one-sentence summary "
        "and the picked agents (each with a short reason).",
    ]
    return "\n".join(lines)


def build_user_message(content: str) -> str:
    return f"Operator's message:\n\n{content}"


def _default_anthropic_factory(api_key: str) -> Any:
    import anthropic
    return anthropic.Anthropic(api_key=api_key, max_retries=5)


def route_team_message(
    session: Session,
    workspace_id: str,
    content: str,
    *,
    anthropic_factory: Optional[Callable[[str], Any]] = None,
    model: str = "claude-opus-4-7",
) -> RouteDecision:
    """Run the router. Returns the decision; raises RouterError on
    config / response problems. Caller is responsible for the
    db writes (router row + per-picked-agent pending assistant rows).
    """
    agents = list_routable_agents(session, workspace_id)
    if not agents:
        return RouteDecision(
            summary="No bots in this workspace yet.",
            agents=[],
        )

    secret = session.get(
        WorkspaceSecret, (workspace_id, "ANTHROPIC_API_KEY"),
    )
    if secret is None:
        raise RouterError("ANTHROPIC_API_KEY missing for this workspace.")
    try:
        api_key = secrets_crypto.decrypt(secret.encrypted_value)
    except Exception:
        raise RouterError("failed to decrypt ANTHROPIC_API_KEY")

    factory = anthropic_factory or _default_anthropic_factory
    client = factory(api_key)

    # Close the read-tx before the LLM call. Railway's idle-in-tx
    # killer takes out connections held across the ~5-30s Anthropic
    # round-trip if we don't (same pattern as team_planner).
    try:
        session.commit()
    except Exception:
        pass

    resp = client.messages.create(
        model=model,
        max_tokens=2048,
        system=build_system_prompt(agents),
        messages=[
            {"role": "user", "content": build_user_message(content)},
        ],
        tools=[ROUTE_TEAM_TOOL],
        tool_choice={"type": "tool", "name": "route_team_message"},
    )

    valid_names = {a["name"] for a in agents}
    for block in (getattr(resp, "content", None) or []):
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", None) == "route_team_message"
        ):
            payload = getattr(block, "input", None) or {}
            summary = str(payload.get("summary") or "").strip()
            raw = payload.get("agents") or []
            picks: list[AgentPick] = []
            seen: set[str] = set()
            for item in raw:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                reason = str(item.get("reason") or "").strip()
                if not name or name in seen:
                    continue
                # Drop hallucinated agent names so the dispatch step
                # never creates a pending row no agent will claim.
                if name not in valid_names:
                    continue
                seen.add(name)
                picks.append(AgentPick(name=name, reason=reason))
            return RouteDecision(summary=summary, agents=picks)

    raise RouterError("router returned no tool_use block")
