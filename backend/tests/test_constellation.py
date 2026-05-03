"""Phase 11B.3: GET /workspaces/me/constellation — drives the home-page
constellation map widget. Returns nodes (agents + role + status + 24h
counters + recent-model) and edges (dispatch graph; empty until 11.2's
dispatch_chain machinery lands).

Coverage focuses on the response shape because the widget itself is
where the business logic for placement / colors / animation lives;
this endpoint just feeds it data.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from db import session_scope
from tests.conftest import auth_headers


def test_constellation_empty_workspace(client, alice):
    """A fresh workspace with zero activity returns empty agents +
    empty edges. No 404 — the home page expects to always render."""
    h = auth_headers(alice["api_key"]["plaintext"])
    r = client.get("/workspaces/me/constellation", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"agents": [], "edges": []}


def test_constellation_lists_agents_with_runs(client, alice):
    """An agent that has emitted at least one run shows up in `agents`
    with status='stopped' (no heartbeat row) and runs_24h reflecting
    the count."""
    h = auth_headers(alice["api_key"]["plaintext"])

    # Two events on the same run for one agent — counts as 1 run.
    client.post(
        "/events",
        json={
            "kind": "run_started",
            "run_id": "run-A",
            "agent_name": "atlas",
            "payload": {},
        },
        headers=h,
    )
    client.post(
        "/events",
        json={
            "kind": "llm_call_completed",
            "run_id": "run-A",
            "agent_name": "atlas",
            "payload": {
                "model": "claude-haiku-4-5",
                "input_tokens": 1000,
                "output_tokens": 200,
            },
        },
        headers=h,
    )

    r = client.get("/workspaces/me/constellation", headers=h)
    body = r.json()
    assert len(body["agents"]) == 1
    a = body["agents"][0]
    assert a["name"] == "atlas"
    assert a["status"] == "stopped"  # no agent_instances row
    assert a["runs_24h"] == 1
    assert a["cost_24h_usd"] > 0
    assert a["model"] == "claude-haiku-4-5"
    # last_event_at populated even when last_heartbeat_at is null.
    assert a["last_event_at"] is not None
    assert a["last_heartbeat_at"] is None


def test_constellation_role_defaults_to_executor_polaris_to_orchestrator(
    client, alice
):
    """The migration auto-tags any agent named 'polaris' as orchestrator
    on the way in. Other names default to 'executor'."""
    h = auth_headers(alice["api_key"]["plaintext"])

    # Auto-create both agents by emitting events for them.
    for name in ("polaris", "atlas"):
        client.post(
            "/events",
            json={
                "kind": "run_started",
                "run_id": f"r-{name}",
                "agent_name": name,
                "payload": {},
            },
            headers=h,
        )

    # The migration auto-tag fires only at upgrade time, so a
    # post-migration insert doesn't get the 'polaris' rule. Mimic
    # that explicitly by patching the column to match what the user's
    # eventual /agents/{name} PATCH (or future autotag in
    # ensure_agent) would do. For Phase 11B.3 this is the contract:
    # the response surfaces whatever the role column says.
    with session_scope() as s:
        s.execute(
            text(
                "UPDATE agents SET role='orchestrator' "
                "WHERE name='polaris'"
            )
        )

    r = client.get("/workspaces/me/constellation", headers=h)
    body = r.json()
    by_name = {a["name"]: a for a in body["agents"]}
    assert by_name["polaris"]["role"] == "orchestrator"
    assert by_name["atlas"]["role"] == "executor"


def test_constellation_status_active_with_recent_heartbeat(client, alice):
    """An agent with a heartbeat in the last 60s renders as 'active'."""
    h = auth_headers(alice["api_key"]["plaintext"])

    # Register the heartbeat via the existing instances endpoint.
    r = client.post(
        "/agents/atlas/instances/heartbeat",
        json={
            "instance_id": "inst-1",
            "hostname": "test-host",
            "pid": 1,
            "sdk_version": "0.0.0",
        },
        headers=h,
    )
    assert r.status_code == 200, r.text

    # Trigger row creation for the agent (heartbeats may not auto-create).
    client.post(
        "/events",
        json={
            "kind": "run_started",
            "run_id": "r-atlas",
            "agent_name": "atlas",
            "payload": {},
        },
        headers=h,
    )

    r = client.get("/workspaces/me/constellation", headers=h)
    body = r.json()
    a = next(x for x in body["agents"] if x["name"] == "atlas")
    assert a["status"] == "active"
    assert a["last_heartbeat_at"] is not None


def test_constellation_status_stale_for_old_heartbeat(client, alice):
    """A heartbeat older than 60s renders as 'stale'."""
    h = auth_headers(alice["api_key"]["plaintext"])

    client.post(
        "/agents/atlas/instances/heartbeat",
        json={"instance_id": "inst-stale", "hostname": "h", "pid": 1, "sdk_version": "0.0.0"},
        headers=h,
    )
    client.post(
        "/events",
        json={"kind": "run_started", "run_id": "r1", "agent_name": "atlas", "payload": {}},
        headers=h,
    )
    # Hand-stamp the heartbeat into the past.
    with session_scope() as s:
        s.execute(
            text(
                "UPDATE agent_instances SET last_heartbeat_at = :old "
                "WHERE agent_name = 'atlas'"
            ),
            {"old": datetime.now(timezone.utc) - timedelta(minutes=10)},
        )

    r = client.get("/workspaces/me/constellation", headers=h)
    body = r.json()
    a = next(x for x in body["agents"] if x["name"] == "atlas")
    assert a["status"] == "stale"


def test_constellation_filters_dormant_non_polaris_agents(client, alice):
    """An agent with no heartbeat AND no runs_24h AND not tagged as
    orchestrator gets filtered off the canvas. We don't want
    placeholder rows from old experiments cluttering the constellation."""
    h = auth_headers(alice["api_key"]["plaintext"])

    # Create an agent row directly with no associated activity.
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        s.execute(
            text(
                """
                INSERT INTO agents
                  (workspace_id, name, role, created_at, updated_at)
                VALUES
                  (:wsid, 'ghost-agent', 'executor', :now, :now)
                """
            ),
            {"wsid": alice["workspace"]["id"], "now": now},
        )

    r = client.get("/workspaces/me/constellation", headers=h)
    body = r.json()
    names = [a["name"] for a in body["agents"]]
    assert "ghost-agent" not in names


def test_constellation_keeps_orchestrator_even_when_dormant(client, alice):
    """Polaris should always show on the canvas — it's the visual
    anchor, even when it hasn't ticked recently."""
    h = auth_headers(alice["api_key"]["plaintext"])

    now = datetime.now(timezone.utc)
    with session_scope() as s:
        s.execute(
            text(
                """
                INSERT INTO agents
                  (workspace_id, name, role, created_at, updated_at)
                VALUES
                  (:wsid, 'polaris', 'orchestrator', :now, :now)
                """
            ),
            {"wsid": alice["workspace"]["id"], "now": now},
        )

    r = client.get("/workspaces/me/constellation", headers=h)
    body = r.json()
    names = [a["name"] for a in body["agents"]]
    assert "polaris" in names


def test_constellation_isolates_across_workspaces(client, alice, bob):
    """Agents in alice's workspace don't leak into bob's response."""
    h_alice = auth_headers(alice["api_key"]["plaintext"])
    h_bob = auth_headers(bob["api_key"]["plaintext"])

    client.post(
        "/events",
        json={
            "kind": "run_started",
            "run_id": "alice-r",
            "agent_name": "atlas",
            "payload": {},
        },
        headers=h_alice,
    )
    r = client.get("/workspaces/me/constellation", headers=h_bob)
    assert r.status_code == 200
    body = r.json()
    assert body["agents"] == []
    assert body["edges"] == []


def test_constellation_edges_empty_in_v1(client, alice):
    """Edges stay empty until Phase 11.2's dispatch_chain machinery
    lands. This pins the v1 contract so the dashboard can rely on it."""
    h = auth_headers(alice["api_key"]["plaintext"])
    client.post(
        "/events",
        json={"kind": "run_started", "run_id": "r1", "agent_name": "atlas", "payload": {}},
        headers=h,
    )
    r = client.get("/workspaces/me/constellation", headers=h)
    body = r.json()
    assert body["edges"] == []
