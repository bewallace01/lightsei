"""Phase 11B.2: GET /workspaces/me/pulse — drives the home-page status hero.

Counts four kinds of "wants your attention" signals (pending dispatch
approvals, failed validations in last 24h, budget warnings ≥ 80%,
stale agent heartbeats > 5 min) and returns a single payload the
hero renders in one read.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from db import session_scope
from tests.conftest import auth_headers


def test_pulse_calm_for_empty_workspace(client, alice):
    """A fresh workspace has zero of every signal — status = 'calm'."""
    h = auth_headers(alice["api_key"]["plaintext"])
    r = client.get("/workspaces/me/pulse", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "calm"
    assert body["issues_count"] == 0
    assert body["issues"] == {
        "pending_approvals": 0,
        "failed_validations": 0,
        "budget_warnings": 0,
        "stale_agents": 0,
    }
    assert body["agent_count"] == 0
    assert body["last_polaris_tick_at"] is None
    assert body["last_event_at"] is None
    assert body["workspace_name"] == "alice-co"


def test_pulse_counts_pending_approvals(client, alice):
    """Each pending dispatch command increments pending_approvals.
    Auto_approved and approved commands don't count — they're past
    the gate."""
    h = auth_headers(alice["api_key"]["plaintext"])
    # Seed polaris.
    client.post(
        "/agents/atlas/commands",
        json={"kind": "atlas.run_tests", "payload": {}, "source_agent": "polaris"},
        headers=h,
    )
    # An agent-driven dispatch lands pending by default.
    client.post(
        "/agents/atlas/commands",
        json={
            "kind": "atlas.run_tests",
            "payload": {"branch": "feature"},
            "source_agent": "polaris",
        },
        headers=h,
    )
    # A user-initiated enqueue auto-approves and shouldn't count.
    client.post(
        "/agents/atlas/commands",
        json={"kind": "atlas.run_tests", "payload": {}},
        headers=h,
    )

    r = client.get("/workspaces/me/pulse", headers=h)
    body = r.json()
    # 2 pending (the seed + the second agent-driven). The user enqueue
    # is auto_approved so it doesn't count.
    assert body["issues"]["pending_approvals"] == 2
    assert body["status"] == "attention"
    assert body["issues_count"] == 2


def test_pulse_budget_warning_at_80_percent(client, alice):
    """Budget warning fires when MTD ≥ 80% of monthly cap. Below
    threshold (or no cap set) returns 0."""
    h = auth_headers(alice["api_key"]["plaintext"])
    # Set a tight budget.
    client.patch(
        "/workspaces/me",
        json={"budget_usd_monthly": 1.0},
        headers=h,
    )
    # Spend $0.85 (85% of $1).
    # claude-opus-4-7: $15/M input + $75/M output.
    # 50000 input * 15 / 1e6 = $0.75. Plus 1000 output * 75 / 1e6 = $0.075. ~$0.825.
    client.post(
        "/events",
        json={
            "kind": "llm_call_completed",
            "run_id": "r1",
            "agent_name": "polaris",
            "payload": {
                "model": "claude-opus-4-7",
                "input_tokens": 50000,
                "output_tokens": 1000,
            },
        },
        headers=h,
    )
    r = client.get("/workspaces/me/pulse", headers=h)
    assert r.json()["issues"]["budget_warnings"] == 1


def test_pulse_no_budget_warning_below_80(client, alice):
    """50% of cap doesn't fire the warning — only 80%+."""
    h = auth_headers(alice["api_key"]["plaintext"])
    client.patch(
        "/workspaces/me",
        json={"budget_usd_monthly": 100.0},
        headers=h,
    )
    # Spend ~$0.825 → way under 80% of $100.
    client.post(
        "/events",
        json={
            "kind": "llm_call_completed",
            "run_id": "r1",
            "agent_name": "polaris",
            "payload": {
                "model": "claude-opus-4-7",
                "input_tokens": 50000,
                "output_tokens": 1000,
            },
        },
        headers=h,
    )
    r = client.get("/workspaces/me/pulse", headers=h)
    assert r.json()["issues"]["budget_warnings"] == 0


def test_pulse_no_budget_warning_when_cap_unset(client, alice):
    """Workspace with no budget_usd_monthly never warns regardless of
    spend."""
    h = auth_headers(alice["api_key"]["plaintext"])
    client.post(
        "/events",
        json={
            "kind": "llm_call_completed",
            "run_id": "r1",
            "agent_name": "polaris",
            "payload": {
                "model": "claude-opus-4-7",
                "input_tokens": 1_000_000,
                "output_tokens": 100_000,
            },
        },
        headers=h,
    )
    r = client.get("/workspaces/me/pulse", headers=h)
    assert r.json()["issues"]["budget_warnings"] == 0


def test_pulse_counts_stale_agents(client, alice):
    """Agents whose latest heartbeat is older than 5 minutes count
    toward stale_agents. Active heartbeats and never-heartbeated
    agents (no instance row) don't count."""
    h = auth_headers(alice["api_key"]["plaintext"])
    # Heartbeat for atlas.
    client.post(
        "/agents/atlas/instances/heartbeat",
        json={
            "instance_id": "inst-stale",
            "hostname": "h",
            "pid": 1,
            "sdk_version": "0.0.0",
        },
        headers=h,
    )
    # Backdate it to >5 min ago.
    with session_scope() as s:
        s.execute(
            text(
                "UPDATE agent_instances SET last_heartbeat_at = :old "
                "WHERE agent_name = 'atlas'"
            ),
            {"old": datetime.now(timezone.utc) - timedelta(minutes=10)},
        )
    r = client.get("/workspaces/me/pulse", headers=h)
    assert r.json()["issues"]["stale_agents"] == 1


def test_pulse_isolates_across_workspaces(client, alice, bob):
    """Pending commands in alice's workspace don't show in bob's pulse."""
    h_alice = auth_headers(alice["api_key"]["plaintext"])
    h_bob = auth_headers(bob["api_key"]["plaintext"])
    client.post(
        "/agents/atlas/commands",
        json={"kind": "atlas.run_tests", "payload": {}, "source_agent": "polaris"},
        headers=h_alice,
    )
    r = client.get("/workspaces/me/pulse", headers=h_bob)
    body = r.json()
    assert body["status"] == "calm"
    assert body["issues_count"] == 0


def test_pulse_last_event_at_drives_pulse_animation(client, alice):
    """`last_event_at` returns the workspace-wide max event timestamp.
    The frontend hero diffs this between polls — a strict-greater
    value means new activity, which triggers the pulsing icon."""
    h = auth_headers(alice["api_key"]["plaintext"])
    r = client.get("/workspaces/me/pulse", headers=h)
    assert r.json()["last_event_at"] is None

    client.post(
        "/events",
        json={
            "kind": "run_started",
            "run_id": "r1",
            "agent_name": "atlas",
            "payload": {},
        },
        headers=h,
    )
    r = client.get("/workspaces/me/pulse", headers=h)
    assert r.json()["last_event_at"] is not None


def test_pulse_polaris_tick_filters_to_polaris_plan_only(client, alice):
    """`last_polaris_tick_at` reflects only `polaris.plan` events,
    not arbitrary polaris activity. A bare run_started for polaris
    doesn't count as a tick."""
    h = auth_headers(alice["api_key"]["plaintext"])
    client.post(
        "/events",
        json={
            "kind": "run_started",
            "run_id": "r1",
            "agent_name": "polaris",
            "payload": {},
        },
        headers=h,
    )
    r = client.get("/workspaces/me/pulse", headers=h)
    assert r.json()["last_polaris_tick_at"] is None
    # Now an actual plan event lands.
    client.post(
        "/events",
        json={
            "kind": "polaris.plan",
            "run_id": "r1",
            "agent_name": "polaris",
            "payload": {"summary": "ok"},
        },
        headers=h,
    )
    r = client.get("/workspaces/me/pulse", headers=h)
    assert r.json()["last_polaris_tick_at"] is not None
