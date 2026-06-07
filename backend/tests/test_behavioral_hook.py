"""Phase 15.3/15.4: behavioral-rule hook on run-end + read endpoint.

Drives the real POST /events ingest path (api-key auth) through a run
that loops and burns runaway tokens, fires run_ended, and asserts the
layer-4 violations were recorded and surface via GET /runs/{id}/behavior.
Also confirms a clean run records nothing and the endpoint is workspace
-scoped.
"""
from __future__ import annotations

import uuid

from tests.conftest import auth_headers, signup


def _key(acct) -> dict[str, str]:
    return auth_headers(acct["api_key"]["plaintext"])


def _emit(client, headers, run_id, kind, payload, agent="polaris"):
    r = client.post(
        "/events",
        headers=headers,
        json={"run_id": run_id, "agent_name": agent, "kind": kind, "payload": payload},
    )
    assert r.status_code == 200, r.text
    return r


def test_run_end_records_loop_and_runaway(client, alice):
    h = _key(alice)
    run_id = str(uuid.uuid4())
    # 6 identical plans (loop, threshold 5) with varying volatile keys.
    for i in range(6):
        _emit(client, h, run_id, "polaris.plan", {"plan": "do the thing", "command_id": f"c{i}"})
    # 300k tokens across the run (runaway, cap 200k).
    for i in range(3):
        _emit(client, h, run_id, "llm_call_completed",
              {"model": "claude-haiku-4-5", "input_tokens": 100_000, "output_tokens": 0})
    # run-end triggers the behavioral evaluation.
    _emit(client, h, run_id, "run_ended", {})

    r = client.get(f"/runs/{run_id}/behavior", headers=h)
    assert r.status_code == 200
    body = r.json()
    rules = {v["rule"]: v["severity"] for v in body["violations"]}
    assert rules.get("loop") == "warn"
    assert rules.get("runaway_tokens") == "block"
    assert body["worst_severity"] == "block"


def test_clean_run_records_nothing(client, alice):
    h = _key(alice)
    run_id = str(uuid.uuid4())
    _emit(client, h, run_id, "polaris.plan", {"plan": "unique plan a"})
    _emit(client, h, run_id, "llm_call_completed",
          {"model": "claude-haiku-4-5", "input_tokens": 100, "output_tokens": 50})
    _emit(client, h, run_id, "run_ended", {})

    r = client.get(f"/runs/{run_id}/behavior", headers=h)
    assert r.status_code == 200
    assert r.json()["worst_severity"] == "none"
    assert r.json()["violations"] == []


def test_re_fired_run_end_does_not_duplicate(client, alice):
    h = _key(alice)
    run_id = str(uuid.uuid4())
    for i in range(6):
        _emit(client, h, run_id, "polaris.plan", {"plan": "loopy", "command_id": f"c{i}"})
    _emit(client, h, run_id, "run_ended", {})
    _emit(client, h, run_id, "run_ended", {})  # second run-end

    r = client.get(f"/runs/{run_id}/behavior", headers=h)
    loops = [v for v in r.json()["violations"] if v["rule"] == "loop"]
    assert len(loops) == 1  # upsert, not duplicate


def test_behavior_endpoint_is_workspace_scoped(client, alice, bob):
    h = _key(alice)
    run_id = str(uuid.uuid4())
    _emit(client, h, run_id, "polaris.plan", {"plan": "x"})
    _emit(client, h, run_id, "run_ended", {})
    # bob can't read alice's run.
    r = client.get(f"/runs/{run_id}/behavior", headers=_key(bob))
    assert r.status_code == 404
