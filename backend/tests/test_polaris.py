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


# ---------- /agents/{name}/plans (history) ---------- #


def test_list_plans_empty(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    r = client.get("/agents/polaris/plans", headers=h)
    assert r.status_code == 200
    assert r.json() == {"plans": []}


def test_list_plans_newest_first(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    _emit_plan(client, h, "polaris", "first")
    time.sleep(0.01)
    _emit_plan(client, h, "polaris", "second")
    time.sleep(0.01)
    _emit_plan(client, h, "polaris", "third")

    r = client.get("/agents/polaris/plans", headers=h)
    assert r.status_code == 200
    plans = r.json()["plans"]
    assert [p["payload"]["summary"] for p in plans] == ["third", "second", "first"]


def test_list_plans_respects_limit(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    for i in range(5):
        _emit_plan(client, h, "polaris", f"plan-{i}")
        time.sleep(0.005)

    r = client.get("/agents/polaris/plans?limit=3", headers=h)
    assert r.status_code == 200
    plans = r.json()["plans"]
    assert len(plans) == 3
    assert plans[0]["payload"]["summary"] == "plan-4"
    assert plans[2]["payload"]["summary"] == "plan-2"


def test_list_plans_validates_limit(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    assert client.get("/agents/polaris/plans?limit=0", headers=h).status_code == 400
    assert client.get("/agents/polaris/plans?limit=101", headers=h).status_code == 400
    # Boundaries are valid.
    assert client.get("/agents/polaris/plans?limit=1", headers=h).status_code == 200
    assert client.get("/agents/polaris/plans?limit=100", headers=h).status_code == 200


def test_list_plans_workspace_isolation(client, alice, bob):
    ha = auth_headers(alice["api_key"]["plaintext"])
    hb = auth_headers(bob["api_key"]["plaintext"])
    _emit_plan(client, ha, "polaris", "alice")
    _emit_plan(client, hb, "polaris", "bob")

    plans_a = client.get("/agents/polaris/plans", headers=ha).json()["plans"]
    plans_b = client.get("/agents/polaris/plans", headers=hb).json()["plans"]
    assert len(plans_a) == 1 and plans_a[0]["payload"]["summary"] == "alice"
    assert len(plans_b) == 1 and plans_b[0]["payload"]["summary"] == "bob"


# ---------- /events/{id}/validations + validations on plan endpoints (Phase 7.4) ---------- #


_TINY_PLAN_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": True,  # plans carry many other fields too
}


def _register_schema_strict(client, headers, kind, schema):
    r = client.put(
        f"/workspaces/me/validators/{kind}/schema_strict",
        json={"config": {"schema": schema}},
        headers=headers,
    )
    assert r.status_code == 200, r.text


def test_event_validations_endpoint_returns_full_violations(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    _register_schema_strict(client, h, "polaris.plan", _TINY_PLAN_SCHEMA)
    _emit_plan(client, h, "polaris", "first plan")

    # Find the polaris.plan event id via the latest-plan endpoint
    latest = client.get("/agents/polaris/latest-plan", headers=h).json()
    event_id = latest["event_id"]

    r = client.get(f"/events/{event_id}/validations", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["event_id"] == event_id
    assert len(body["validations"]) == 1
    v = body["validations"][0]
    assert v["validator"] == "schema_strict"
    assert v["status"] == "pass"
    assert v["violations"] == []


def test_event_validations_endpoint_404_when_not_in_workspace(client, alice, bob):
    """Bob can't read alice's event-validation rows even if he guesses
    the event id."""
    ha = auth_headers(alice["api_key"]["plaintext"])
    hb = auth_headers(bob["api_key"]["plaintext"])
    _emit_plan(client, ha, "polaris", "alice plan")
    latest = client.get("/agents/polaris/latest-plan", headers=ha).json()
    event_id = latest["event_id"]

    # Same id, but bob's auth — must 404, no leak via timing or detail.
    r = client.get(f"/events/{event_id}/validations", headers=hb)
    assert r.status_code == 404
    assert r.json()["detail"] == "event not found"


def test_event_validations_endpoint_404_when_event_unknown(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    r = client.get("/events/99999999/validations", headers=h)
    assert r.status_code == 404


def test_latest_plan_includes_full_validations(client, alice):
    """The single-plan endpoint embeds full violations (no follow-up
    fetch needed for the default-selected plan)."""
    h = auth_headers(alice["api_key"]["plaintext"])
    _register_schema_strict(client, h, "polaris.plan", _TINY_PLAN_SCHEMA)
    _emit_plan(client, h, "polaris", "ok plan")

    body = client.get("/agents/polaris/latest-plan", headers=h).json()
    assert "validations" in body
    assert len(body["validations"]) == 1
    v = body["validations"][0]
    assert v["validator"] == "schema_strict"
    assert v["status"] == "pass"
    # Full violations included on this endpoint (no violation_count field)
    assert "violations" in v
    assert "violation_count" not in v


def test_list_plans_includes_validation_summaries_only(client, alice):
    """The list endpoint trims to summary chips: validator + status +
    violation_count, no full violation details."""
    h = auth_headers(alice["api_key"]["plaintext"])
    _register_schema_strict(client, h, "polaris.plan", _TINY_PLAN_SCHEMA)
    _emit_plan(client, h, "polaris", "p1")
    _emit_plan(client, h, "polaris", "p2")

    plans = client.get("/agents/polaris/plans", headers=h).json()["plans"]
    assert len(plans) == 2
    for p in plans:
        assert "validations" in p
        for v in p["validations"]:
            # Summary fields present
            assert set(v.keys()) == {"validator", "status", "violation_count"}


def test_list_plans_violation_count_reflects_actual_count(client, alice):
    """A failing plan -> violation_count > 0 in the summary."""
    import uuid as _uuid

    h = auth_headers(alice["api_key"]["plaintext"])
    _register_schema_strict(client, h, "polaris.plan", _TINY_PLAN_SCHEMA)

    # Manually build a polaris.plan event missing the required `summary`.
    run_id = str(_uuid.uuid4())
    _emit_event(client, h, run_id, "polaris", "run_started")
    _emit_event(
        client, h, run_id, "polaris", "polaris.plan",
        {
            "text": "x",
            "doc_hashes": {"memory_md": "x", "tasks_md": "y"},
            "model": "claude-opus-4-7",
            "tokens_in": 1, "tokens_out": 1,
            # summary deliberately absent
            "next_actions": [], "parking_lot_promotions": [], "drift": [],
        },
    )
    _emit_event(client, h, run_id, "polaris", "run_ended")

    plans = client.get("/agents/polaris/plans", headers=h).json()["plans"]
    assert len(plans) == 1
    summary = plans[0]["validations"][0]
    assert summary["status"] == "fail"
    assert summary["violation_count"] == 1


def test_list_plans_validations_empty_when_no_validators_registered(client, alice):
    """Plans emitted with no validator config registered carry an empty
    validations array — distinguishable from 'all passed' by the array
    being empty rather than containing pass entries."""
    h = auth_headers(alice["api_key"]["plaintext"])
    _emit_plan(client, h, "polaris", "no validators here")

    plans = client.get("/agents/polaris/plans", headers=h).json()["plans"]
    assert plans[0]["validations"] == []


def test_latest_plan_validations_workspace_isolation(client, alice, bob):
    """Alice's validators don't leak into bob's latest-plan response."""
    ha = auth_headers(alice["api_key"]["plaintext"])
    hb = auth_headers(bob["api_key"]["plaintext"])
    _register_schema_strict(client, ha, "polaris.plan", _TINY_PLAN_SCHEMA)
    _emit_plan(client, ha, "polaris", "alice")
    _emit_plan(client, hb, "polaris", "bob")

    alice_plan = client.get("/agents/polaris/latest-plan", headers=ha).json()
    bob_plan = client.get("/agents/polaris/latest-plan", headers=hb).json()
    assert len(alice_plan["validations"]) == 1
    assert bob_plan["validations"] == []  # bob never registered the validator
