"""Tests for Phase 6.3: GET /agents/{agent_name}/latest-plan.

The endpoint reads back the most recent `polaris.plan` event for an
agent in the calling workspace. Verified here:
- 404 when no plan events exist for the agent.
- 200 with the full payload when one does.
- Latest wins when multiple plans have been emitted (ordered by
  timestamp DESC then id DESC for ties).
- Cross-workspace isolation: alice can't read bob's plan even via
  the same agent name.
- Non-polaris event kinds (run_started, custom events) don't count.
- The endpoint doesn't require the agent name to be `polaris`.
"""
import time
import uuid

from tests.conftest import auth_headers


def _emit_event(client, headers, run_id, agent_name, kind, payload=None):
    r = client.post(
        "/events",
        json={
            "run_id": run_id,
            "agent_name": agent_name,
            "kind": kind,
            "payload": payload or {},
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text


def _emit_plan(client, headers, agent_name, summary):
    """Write a complete polaris.plan event with a fresh run_id."""
    run_id = str(uuid.uuid4())
    _emit_event(client, headers, run_id, agent_name, "run_started")
    _emit_event(
        client, headers, run_id, agent_name, "polaris.plan",
        {
            "text": "raw text",
            "doc_hashes": {"memory_md": "abc123", "tasks_md": "def456"},
            "model": "claude-opus-4-7",
            "tokens_in": 1234,
            "tokens_out": 567,
            "summary": summary,
            "next_actions": [
                {"task": "do x", "why": "because", "blocked_by": None},
            ],
            "parking_lot_promotions": [],
            "drift": [],
        },
    )
    _emit_event(client, headers, run_id, agent_name, "run_ended")
    return run_id


def test_latest_plan_404_when_no_events(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    r = client.get("/agents/polaris/latest-plan", headers=h)
    assert r.status_code == 404
    assert r.json()["detail"] == "no plan yet"


def test_latest_plan_returns_payload(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    _emit_plan(client, h, "polaris", "first plan")

    r = client.get("/agents/polaris/latest-plan", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["agent_name"] == "polaris"
    assert body["payload"]["summary"] == "first plan"
    assert body["payload"]["model"] == "claude-opus-4-7"
    assert body["payload"]["doc_hashes"]["memory_md"] == "abc123"
    assert body["payload"]["next_actions"][0]["task"] == "do x"
    assert body["run_id"]
    assert body["timestamp"]
    assert isinstance(body["event_id"], int)


def test_latest_plan_returns_most_recent(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    _emit_plan(client, h, "polaris", "older plan")
    # Sleep briefly so timestamps strictly differ. Postgres timestamptz
    # has microsecond resolution which is more than enough.
    time.sleep(0.01)
    _emit_plan(client, h, "polaris", "newer plan")

    r = client.get("/agents/polaris/latest-plan", headers=h)
    assert r.status_code == 200
    assert r.json()["payload"]["summary"] == "newer plan"


def test_latest_plan_ignores_other_kinds(client, alice):
    """A run with run_started + tick_skipped + run_ended (no polaris.plan)
    must still 404."""
    h = auth_headers(alice["api_key"]["plaintext"])
    run_id = str(uuid.uuid4())
    _emit_event(client, h, run_id, "polaris", "run_started")
    _emit_event(
        client, h, run_id, "polaris", "polaris.tick_skipped",
        {"reason": "docs unchanged", "hashes": {"memory_md": "x", "tasks_md": "y"}},
    )
    _emit_event(client, h, run_id, "polaris", "run_ended")

    r = client.get("/agents/polaris/latest-plan", headers=h)
    assert r.status_code == 404


def test_latest_plan_workspace_isolation(client, alice, bob):
    """Alice and bob both have an agent called `polaris` with their own
    plans. Each only sees their own."""
    ha = auth_headers(alice["api_key"]["plaintext"])
    hb = auth_headers(bob["api_key"]["plaintext"])

    _emit_plan(client, ha, "polaris", "alice plan")
    _emit_plan(client, hb, "polaris", "bob plan")

    ra = client.get("/agents/polaris/latest-plan", headers=ha)
    rb = client.get("/agents/polaris/latest-plan", headers=hb)
    assert ra.status_code == 200
    assert rb.status_code == 200
    assert ra.json()["payload"]["summary"] == "alice plan"
    assert rb.json()["payload"]["summary"] == "bob plan"


def test_latest_plan_works_for_any_agent_name(client, alice):
    """The endpoint doesn't require the agent to be named `polaris`."""
    h = auth_headers(alice["api_key"]["plaintext"])
    _emit_plan(client, h, "ceo-bot", "from a non-polaris-named agent")

    r = client.get("/agents/ceo-bot/latest-plan", headers=h)
    assert r.status_code == 200
    assert r.json()["payload"]["summary"] == "from a non-polaris-named agent"

    # Different agent name in same workspace → still 404 for that one.
    r = client.get("/agents/some-other-bot/latest-plan", headers=h)
    assert r.status_code == 404


def test_latest_plan_unauthorized(client):
    """No auth header → 401."""
    r = client.get("/agents/polaris/latest-plan")
    assert r.status_code == 401
