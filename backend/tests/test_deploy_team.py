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


def _agent_caps(ws_id: str, agent_name: str) -> list[str]:
    with session_scope() as s:
        row = s.execute(
            text("SELECT capabilities FROM agents WHERE workspace_id = :ws "
                 "AND name = :a"),
            {"ws": ws_id, "a": agent_name},
        ).first()
        return list(row[0] or []) if row else []


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


def test_deploy_team_repairs_builtin_capabilities(client, alice):
    ws_id = alice["workspace"]["id"]
    _provision(ws_id, "website", "lead")
    h = auth_headers(alice["session_token"])

    client.post("/workspaces/me/team/deploy", headers=h)

    assert _agent_caps(ws_id, "website") == ["internet", "send_command"]
    assert _agent_caps(ws_id, "lead") == ["send_command"]


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


def test_team_status_reports_per_assistant_state(client, alice):
    ws_id = alice["workspace"]["id"]
    _provision(ws_id, "bi", "lead")
    h = auth_headers(alice["session_token"])

    # Before deploy: provisioned but no deployment -> status null.
    body = client.get("/workspaces/me/team/status", headers=h).json()
    by_name = {a["name"]: a for a in body["assistants"]}
    assert set(by_name) == {"bi", "lead"}
    assert by_name["bi"]["status"] is None
    assert by_name["bi"]["deployed"] is False
    assert by_name["bi"]["is_llm"] is True
    assert by_name["lead"]["is_llm"] is False
    assert body["needs_anthropic_key"] is True  # bi is LLM, no key

    # After deploy: queued.
    client.post("/workspaces/me/team/deploy", headers=h)
    body = client.get("/workspaces/me/team/status", headers=h).json()
    by_name = {a["name"]: a for a in body["assistants"]}
    assert by_name["bi"]["status"] == "queued"
    assert by_name["bi"]["deployed"] is True
    assert by_name["bi"]["running"] is False


def test_team_status_empty_without_provisioned_personas(client, alice):
    h = auth_headers(alice["session_token"])
    body = client.get("/workspaces/me/team/status", headers=h).json()
    assert body["assistants"] == []
    assert body["needs_anthropic_key"] is False


def test_deploy_team_provisions_lightsei_api_key_secret(client, alice):
    """Without a LIGHTSEI_API_KEY secret, deployed bots refuse to start
    ('LIGHTSEI_API_KEY missing'). Deploy must mint + store one so the team
    actually runs for onboarded (key-less) users."""
    ws_id = alice["workspace"]["id"]
    _provision(ws_id, "website")
    h = auth_headers(alice["session_token"])

    with session_scope() as s:
        assert s.get(WorkspaceSecret, (ws_id, "LIGHTSEI_API_KEY")) is None

    client.post("/workspaces/me/team/deploy", headers=h)

    with session_scope() as s:
        assert s.get(WorkspaceSecret, (ws_id, "LIGHTSEI_API_KEY")) is not None

    # The worker secrets endpoint now serves it (decrypted) so the bot's env
    # has it. This is the env var the bots check on startup.
    secrets = client.get(
        f"/worker/workspaces/{ws_id}/secrets",
        headers={"Authorization": "Bearer test-worker-token"},
    ).json()["secrets"]
    assert secrets.get("LIGHTSEI_API_KEY")  # present + non-empty


def _dep_count(ws_id, agent):
    with session_scope() as s:
        return s.execute(
            text("SELECT count(*) FROM deployments WHERE workspace_id=:ws "
                 "AND agent_name=:a"),
            {"ws": ws_id, "a": agent},
        ).scalar_one()


def test_adding_anthropic_key_auto_redeploys_ai_assistants(client, alice):
    """A non-technical owner shouldn't redeploy bots by hand. Adding the
    ANTHROPIC_API_KEY secret auto-redeploys the running AI personas so they
    pick it up (secrets inject at spawn). Heuristic personas are left
    alone."""
    ws_id = alice["workspace"]["id"]
    _provision(ws_id, "bi", "website")  # bi = LLM, website = heuristic
    h = auth_headers(alice["session_token"])
    client.post("/workspaces/me/team/deploy", headers=h)

    bi_before = _dep_count(ws_id, "bi")
    web_before = _dep_count(ws_id, "website")

    r = client.put("/workspaces/me/secrets/ANTHROPIC_API_KEY", headers=h,
                   json={"value": "sk-ant-test"})
    assert r.status_code == 200, r.text
    assert "bi" in r.json()["redeployed_assistants"]
    assert "website" not in r.json()["redeployed_assistants"]

    # A fresh bi deployment was queued; website (heuristic) untouched.
    assert _dep_count(ws_id, "bi") == bi_before + 1
    assert _dep_count(ws_id, "website") == web_before


def test_setting_unrelated_secret_does_not_redeploy(client, alice):
    ws_id = alice["workspace"]["id"]
    _provision(ws_id, "bi")
    h = auth_headers(alice["session_token"])
    client.post("/workspaces/me/team/deploy", headers=h)
    bi_before = _dep_count(ws_id, "bi")

    r = client.put("/workspaces/me/secrets/SOME_OTHER_KEY", headers=h,
                   json={"value": "x"})
    assert r.json().get("redeployed_assistants") == []
    assert _dep_count(ws_id, "bi") == bi_before


def test_deploy_team_does_not_duplicate_api_key(client, alice):
    ws_id = alice["workspace"]["id"]
    _provision(ws_id, "website")
    h = auth_headers(alice["session_token"])

    client.post("/workspaces/me/team/deploy", headers=h)
    client.post("/workspaces/me/team/deploy", headers=h)  # idempotent

    with session_scope() as s:
        n = s.execute(
            text("SELECT count(*) FROM api_keys WHERE workspace_id = :ws "
                 "AND name = 'team (auto)'"),
            {"ws": ws_id},
        ).scalar_one()
        assert n == 1
