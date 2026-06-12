"""Phase 33.2: POST /workspaces/me/team/deploy tests.

Deploys the provisioned built-in personas (assistant rows) by queuing
their vendored bundles. The TestClient doesn't run the worker, so
deployments just sit 'queued' — which is exactly what we assert.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text

from db import session_scope
from models import Agent, WorkspaceSecret
from tests.conftest import auth_headers


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _provision(ws_id: str, *names: str) -> None:
    with session_scope() as s:
        for n in names:
            s.add(Agent(workspace_id=ws_id, name=n, role="executor",
                        created_at=_now(), updated_at=_now()))


def _deployment_count(ws_id: str, agent_name: str) -> int:
    with session_scope() as s:
        return s.execute(
            text("SELECT count(*) FROM deployments WHERE workspace_id = :ws "
                 "AND agent_name = :a AND status = 'queued'"),
            {"ws": ws_id, "a": agent_name},
        ).scalar_one()


def test_deploy_team_queues_provisioned_personas(client, alice):
    ws_id = alice["workspace"]["id"]
    _provision(ws_id, "bi", "lead")
    h = auth_headers(alice["session_token"])

    r = client.post("/workspaces/me/team/deploy", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["deployed"]) == {"bi", "lead"}
    assert _deployment_count(ws_id, "bi") == 1
    assert _deployment_count(ws_id, "lead") == 1


def test_deploy_team_is_idempotent(client, alice):
    ws_id = alice["workspace"]["id"]
    _provision(ws_id, "reputation")
    h = auth_headers(alice["session_token"])

    first = client.post("/workspaces/me/team/deploy", headers=h).json()
    assert first["deployed"] == ["reputation"]

    second = client.post("/workspaces/me/team/deploy", headers=h).json()
    assert second["deployed"] == []
    assert second["already_running"] == ["reputation"]
    # No second deployment row for the same agent.
    assert _deployment_count(ws_id, "reputation") == 1


def test_deploy_team_flags_missing_anthropic_key(client, alice):
    ws_id = alice["workspace"]["id"]
    _provision(ws_id, "bi")  # bi is LLM-backed
    h = auth_headers(alice["session_token"])

    body = client.post("/workspaces/me/team/deploy", headers=h).json()
    assert body["needs_anthropic_key"] is True


def test_deploy_team_no_key_flag_when_key_present(client, alice):
    ws_id = alice["workspace"]["id"]
    _provision(ws_id, "bi")
    with session_scope() as s:
        s.add(WorkspaceSecret(
            workspace_id=ws_id, name="ANTHROPIC_API_KEY",
            encrypted_value="x", created_at=_now(), updated_at=_now(),
        ))
    h = auth_headers(alice["session_token"])

    body = client.post("/workspaces/me/team/deploy", headers=h).json()
    assert body["needs_anthropic_key"] is False


def test_deploy_team_ignores_non_builtin_agents(client, alice):
    ws_id = alice["workspace"]["id"]
    _provision(ws_id, "argus")  # a dev bot, not a business persona
    h = auth_headers(alice["session_token"])

    body = client.post("/workspaces/me/team/deploy", headers=h).json()
    assert body["deployed"] == []
    assert body["needs_anthropic_key"] is False
