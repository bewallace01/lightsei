"""Phase 22.8: tests for the /runs trigger_id filter + badge fields.

GET /runs now joins triggers so each row carries:
- triggered_by_trigger_id (FK, NULL on manual + after trigger delete)
- trigger_kind (text snapshot, survives trigger delete so the
  /runs badge still renders)
- trigger_name (left-join lookup, NULL when trigger is gone)

The ?trigger_id= filter scopes the list to one trigger's runs.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from db import session_scope
from models import Agent, Run, Trigger, Workspace
from tests.conftest import auth_headers


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _add_agent(workspace_id: str, name: str = "morning-digest") -> None:
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


def _create_trigger(client, alice, *, name: str) -> str:
    r = client.post(
        "/agents/morning-digest/triggers",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"kind": "cron", "name": name, "preset": "daily"},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _seed_run(
    workspace_id: str,
    *,
    agent_name: str = "morning-digest",
    trigger_id: str | None = None,
    trigger_kind: str | None = None,
    started_at: datetime | None = None,
) -> str:
    run_id = str(uuid.uuid4())
    now = started_at or _now()
    with session_scope() as s:
        s.add(Run(
            id=run_id,
            workspace_id=workspace_id,
            agent_name=agent_name,
            started_at=now,
            ended_at=now,
            cost_usd=Decimal("0"),
            triggered_by_trigger_id=trigger_id,
            trigger_kind=trigger_kind,
        ))
    return run_id


# ---------- Badge fields on every row ---------- #


def test_runs_row_includes_trigger_fields_for_manual_run(client, alice):
    _add_agent(alice["workspace"]["id"])
    _seed_run(alice["workspace"]["id"])

    r = client.get("/runs", headers=auth_headers(alice["api_key"]["plaintext"]))
    assert r.status_code == 200
    rows = r.json()["runs"]
    assert len(rows) == 1
    row = rows[0]
    assert row["triggered_by_trigger_id"] is None
    assert row["trigger_kind"] is None
    assert row["trigger_name"] is None


def test_runs_row_includes_trigger_fields_for_triggered_run(client, alice):
    _add_agent(alice["workspace"]["id"])
    tid = _create_trigger(client, alice, name="morning")
    _seed_run(
        alice["workspace"]["id"],
        trigger_id=tid, trigger_kind="cron",
    )

    r = client.get("/runs", headers=auth_headers(alice["api_key"]["plaintext"]))
    rows = r.json()["runs"]
    assert len(rows) == 1
    assert rows[0]["triggered_by_trigger_id"] == tid
    assert rows[0]["trigger_kind"] == "cron"
    assert rows[0]["trigger_name"] == "morning"


def test_runs_row_keeps_trigger_kind_after_trigger_delete(client, alice):
    """FK is SET NULL on trigger delete, but the trigger_kind
    snapshot stays so the /runs badge still renders. trigger_name
    becomes null (no row to join)."""
    _add_agent(alice["workspace"]["id"])
    tid = _create_trigger(client, alice, name="morning")
    _seed_run(
        alice["workspace"]["id"],
        trigger_id=tid, trigger_kind="cron",
    )

    # Operator deletes the trigger.
    r = client.delete(
        f"/triggers/{tid}",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 200

    r = client.get("/runs", headers=auth_headers(alice["api_key"]["plaintext"]))
    rows = r.json()["runs"]
    assert len(rows) == 1
    assert rows[0]["triggered_by_trigger_id"] is None  # FK nulled
    assert rows[0]["trigger_kind"] == "cron"  # snapshot survives
    assert rows[0]["trigger_name"] is None  # no row to join


# ---------- ?trigger_id= filter ---------- #


def test_filter_returns_only_runs_from_that_trigger(client, alice):
    _add_agent(alice["workspace"]["id"])
    t_a = _create_trigger(client, alice, name="a")
    t_b = _create_trigger(client, alice, name="b")
    _seed_run(alice["workspace"]["id"], trigger_id=t_a, trigger_kind="cron")
    _seed_run(alice["workspace"]["id"], trigger_id=t_b, trigger_kind="cron")
    _seed_run(alice["workspace"]["id"])  # manual

    r = client.get(
        f"/runs?trigger_id={t_a}",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    rows = r.json()["runs"]
    assert len(rows) == 1
    assert rows[0]["trigger_name"] == "a"


def test_filter_with_no_matching_runs_returns_empty(client, alice):
    _add_agent(alice["workspace"]["id"])
    tid = _create_trigger(client, alice, name="lonely")
    # No runs seeded against the trigger.

    r = client.get(
        f"/runs?trigger_id={tid}",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.json()["runs"] == []


def test_filter_does_not_leak_across_workspaces(client, alice, bob):
    """Alice filtering by a trigger_id that belongs to bob's
    workspace gets an empty list (the workspace_id scope wins
    before the trigger_id filter is even consulted)."""
    _add_agent(bob["workspace"]["id"])
    bob_tid = _create_trigger(client, bob, name="bob-trigger")
    _seed_run(
        bob["workspace"]["id"], trigger_id=bob_tid, trigger_kind="cron",
    )

    r = client.get(
        f"/runs?trigger_id={bob_tid}",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 200
    assert r.json()["runs"] == []
