"""Phase 30.4.a: tests for GET /runs?with_summary=true.

The flag adds an N+1-killer for clients that previously fetched each
run's events to compute model / tokens / latency / denial themselves
(this is what dashboard/app/api.ts summarize() does on the web; the
iOS app would copy that pattern without this flag and burn 50+
round-trips per Runs-list refresh).

Surfaces:

1. Default response (no flag) is unchanged: id / agent_name /
   timestamps / trigger_* only — no summary fields. Regression guard
   for any client that ignores extra keys.
2. with_summary=true inlines model / input_tokens / output_tokens /
   latency_ms / event_count / denied / denial.
3. Summary aggregation rules match dashboard/app/api.ts summarize():
   - latest model wins
   - tokens sum across llm_call_completed events
   - latency = sum(duration_s) * 1000, rounded
   - first policy_denied row wins for denial payload
   - event_count is total events (not filtered)
4. Runs with no events get zero/empty fields, not null/error.
5. Workspace isolation: with_summary aggregation does NOT pull events
   from a sibling workspace's runs.
6. trigger_id filter still works alongside with_summary=true.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from db import session_scope
from models import Agent, Event, Run
from tests.conftest import auth_headers


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _add_agent(workspace_id: str, name: str = "argus") -> None:
    with session_scope() as s:
        s.add(Agent(
            workspace_id=workspace_id,
            name=name,
            role="executor",
            capabilities=[],
            command_handlers=[],
            created_at=_now(),
            updated_at=_now(),
        ))


def _seed_run(
    workspace_id: str, *, agent_name: str = "argus",
    trigger_id: str | None = None,
) -> str:
    run_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(Run(
            id=run_id,
            workspace_id=workspace_id,
            agent_name=agent_name,
            started_at=_now(),
            ended_at=_now(),
            cost_usd=Decimal("0"),
            triggered_by_trigger_id=trigger_id,
        ))
    return run_id


def _seed_event(
    workspace_id: str, run_id: str, kind: str, *,
    agent_name: str = "argus",
    payload: dict | None = None,
) -> None:
    with session_scope() as s:
        s.add(Event(
            workspace_id=workspace_id,
            run_id=run_id,
            agent_name=agent_name,
            kind=kind,
            payload=payload or {},
            timestamp=_now(),
        ))


# ---------- Default shape (no flag) ---------- #


def test_default_response_omits_summary_fields(client, alice):
    _add_agent(alice["workspace"]["id"])
    rid = _seed_run(alice["workspace"]["id"])
    _seed_event(
        alice["workspace"]["id"], rid, "llm_call_completed",
        payload={"model": "claude-opus-4-7",
                 "input_tokens": 100, "output_tokens": 50,
                 "duration_s": 1.5},
    )

    r = client.get(
        "/runs", headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 200
    rows = r.json()["runs"]
    assert len(rows) == 1
    row = rows[0]
    # The basic fields are present.
    assert row["id"] == rid
    assert row["agent_name"] == "argus"
    # No summary fields without the flag.
    for k in ("model", "input_tokens", "output_tokens",
              "latency_ms", "event_count", "denied", "denial"):
        assert k not in row


# ---------- with_summary aggregation ---------- #


def test_with_summary_sums_tokens_and_latency_across_llm_events(
    client, alice,
):
    _add_agent(alice["workspace"]["id"])
    rid = _seed_run(alice["workspace"]["id"])
    # Two LLM calls in one run.
    _seed_event(
        alice["workspace"]["id"], rid, "llm_call_completed",
        payload={"model": "claude-sonnet-4-6",
                 "input_tokens": 100, "output_tokens": 40,
                 "duration_s": 1.2},
    )
    _seed_event(
        alice["workspace"]["id"], rid, "llm_call_completed",
        payload={"model": "claude-opus-4-7",
                 "input_tokens": 200, "output_tokens": 80,
                 "duration_s": 2.3},
    )

    r = client.get(
        "/runs?with_summary=true",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 200
    row = r.json()["runs"][0]
    # Latest model wins (matches web summarize()).
    assert row["model"] == "claude-opus-4-7"
    assert row["input_tokens"] == 300
    assert row["output_tokens"] == 120
    # (1.2 + 2.3) * 1000 = 3500
    assert row["latency_ms"] == 3500
    assert row["event_count"] == 2
    assert row["denied"] is False
    assert row["denial"] is None


def test_with_summary_captures_first_denial(client, alice):
    _add_agent(alice["workspace"]["id"])
    rid = _seed_run(alice["workspace"]["id"])
    _seed_event(
        alice["workspace"]["id"], rid, "policy_denied",
        payload={
            "policy": "daily_cost_cap",
            "reason": "over budget",
            "cap_usd": 5.0,
            "cost_so_far_usd": 5.1,
            "action": "block",
        },
    )
    # A second denial later in the run is ignored (first wins).
    _seed_event(
        alice["workspace"]["id"], rid, "policy_denied",
        payload={
            "policy": "later_policy",
            "reason": "shouldn't appear",
            "action": "block",
        },
    )

    r = client.get(
        "/runs?with_summary=true",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    row = r.json()["runs"][0]
    assert row["denied"] is True
    d = row["denial"]
    assert d["policy"] == "daily_cost_cap"
    assert d["reason"] == "over budget"
    assert d["cap_usd"] == 5.0
    assert d["cost_so_far_usd"] == 5.1
    assert d["action"] == "block"


def test_with_summary_run_with_no_events_returns_zero_fields(
    client, alice,
):
    _add_agent(alice["workspace"]["id"])
    _seed_run(alice["workspace"]["id"])

    r = client.get(
        "/runs?with_summary=true",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    row = r.json()["runs"][0]
    assert row["model"] is None
    assert row["input_tokens"] == 0
    assert row["output_tokens"] == 0
    assert row["latency_ms"] == 0
    assert row["event_count"] == 0
    assert row["denied"] is False
    assert row["denial"] is None


def test_with_summary_counts_all_event_kinds(client, alice):
    """event_count is total events, not filtered to llm/denied.
    Matches dashboard/app/api.ts where events.length is used."""
    _add_agent(alice["workspace"]["id"])
    rid = _seed_run(alice["workspace"]["id"])
    _seed_event(
        alice["workspace"]["id"], rid, "llm_call_completed",
        payload={"model": "x", "input_tokens": 1,
                 "output_tokens": 1, "duration_s": 0.1},
    )
    # Two unrelated events that should still count.
    _seed_event(alice["workspace"]["id"], rid, "command_started")
    _seed_event(alice["workspace"]["id"], rid, "command_completed")

    r = client.get(
        "/runs?with_summary=true",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.json()["runs"][0]["event_count"] == 3


# ---------- Isolation + filter compat ---------- #


def test_with_summary_does_not_pull_other_workspaces_events(
    client, alice, bob,
):
    """Defensive: if a regression in the aggregator dropped the
    workspace_id filter on events, alice's row would show bob's
    tokens. Catches that immediately."""
    _add_agent(alice["workspace"]["id"])
    _add_agent(bob["workspace"]["id"])
    alice_run = _seed_run(alice["workspace"]["id"])
    bob_run = _seed_run(bob["workspace"]["id"])

    # Bob's event has the SAME run_id as alice's run (impossible in
    # prod, but we want the filter to defend against it anyway).
    _seed_event(
        bob["workspace"]["id"], alice_run, "llm_call_completed",
        payload={"model": "leaked", "input_tokens": 9999,
                 "output_tokens": 9999, "duration_s": 99.0},
    )
    # And a real event on alice's run for sanity.
    _seed_event(
        alice["workspace"]["id"], alice_run, "llm_call_completed",
        payload={"model": "alice-real", "input_tokens": 10,
                 "output_tokens": 5, "duration_s": 0.1},
    )

    r = client.get(
        "/runs?with_summary=true",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    row = next(r for r in r.json()["runs"] if r["id"] == alice_run)
    assert row["model"] == "alice-real"
    assert row["input_tokens"] == 10
    assert row["output_tokens"] == 5


def test_trigger_id_filter_still_works_with_summary(
    client, alice, monkeypatch,
):
    _add_agent(alice["workspace"]["id"])
    # One run with a trigger + one without.
    h = auth_headers(alice["api_key"]["plaintext"])
    t = client.post(
        "/agents/argus/triggers", headers=h,
        json={"kind": "cron", "name": "daily-scan", "preset": "daily"},
    )
    assert t.status_code == 200, t.text
    trigger_id = t.json()["id"]
    triggered_run = _seed_run(
        alice["workspace"]["id"], trigger_id=trigger_id,
    )
    _seed_run(alice["workspace"]["id"])  # manual run, no trigger
    _seed_event(
        alice["workspace"]["id"], triggered_run, "llm_call_completed",
        payload={"model": "x", "input_tokens": 5,
                 "output_tokens": 5, "duration_s": 0.5},
    )

    r = client.get(
        f"/runs?with_summary=true&trigger_id={trigger_id}", headers=h,
    )
    rows = r.json()["runs"]
    assert len(rows) == 1
    assert rows[0]["id"] == triggered_run
    assert rows[0]["model"] == "x"
    assert rows[0]["input_tokens"] == 5
