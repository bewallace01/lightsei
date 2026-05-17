"""Phase 16.1: tests for sensitivity_level schema + validator.

Three things under test:

1. The `is_valid_sensitivity_level` helper (pure: enum-tight string,
   rejects None / non-str / off-list).
2. Default behavior at the DB layer — inserting an Agent or Run
   without specifying a level lands on `'internal'`.
3. Round-trip: a non-default level set explicitly survives a
   commit + reload.

The actual cross-zone enforcement that uses this column lives in 16.4
and gets its own test file. This sub-task is just the schema backbone.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from db import session_scope
from models import (
    DEFAULT_SENSITIVITY_LEVEL,
    _VALID_SENSITIVITY_LEVELS,
    Agent,
    Run,
    is_valid_sensitivity_level,
)


# ---------- Helpers ---------- #


def _make_agent(session, workspace_id, *, name, **kwargs):
    """Insert an Agent row with sensible defaults so tests can focus on
    the field they're exercising."""
    now = datetime.now(timezone.utc)
    session.add(
        Agent(
            workspace_id=workspace_id,
            name=name,
            role="executor",
            created_at=now,
            updated_at=now,
            **kwargs,
        )
    )


def _make_run(session, workspace_id, *, agent_name, **kwargs):
    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    session.add(
        Run(
            id=run_id,
            workspace_id=workspace_id,
            agent_name=agent_name,
            started_at=now,
            ended_at=None,
            cost_usd=Decimal("0"),
            **kwargs,
        )
    )
    return run_id


# ---------- is_valid_sensitivity_level ---------- #


def test_valid_levels_are_recognized():
    """The four levels in the ladder are all accepted."""
    for level in ("public", "internal", "sensitive", "pii"):
        assert is_valid_sensitivity_level(level), level


def test_off_list_levels_are_rejected():
    """Strings outside the ladder return False; the endpoint /SDK layer
    that calls this decides whether to default or 4xx."""
    for bad in ("PUBLIC", "Internal", "secret", "high", ""):
        assert not is_valid_sensitivity_level(bad), bad


def test_non_string_inputs_are_rejected():
    """None, ints, lists, dicts all fail the type check cleanly so a
    misuse can't crash the caller."""
    for bad in (None, 0, 1, [], {}, ["pii"]):
        assert not is_valid_sensitivity_level(bad), repr(bad)


def test_valid_levels_set_is_frozen():
    """Documenting the invariant that the level set isn't mutable at
    runtime — anyone reaching for `.add()` on a frozenset gets caught."""
    assert isinstance(_VALID_SENSITIVITY_LEVELS, frozenset)
    with pytest.raises(AttributeError):
        _VALID_SENSITIVITY_LEVELS.add("foo")  # type: ignore[attr-defined]


def test_default_level_is_in_the_valid_set():
    """Defense against typos in DEFAULT_SENSITIVITY_LEVEL drifting from
    the valid set."""
    assert DEFAULT_SENSITIVITY_LEVEL in _VALID_SENSITIVITY_LEVELS


# ---------- Agent: DB column behavior ---------- #


def test_agent_defaults_to_internal_when_unspecified(client, alice):
    """A fresh agent row that doesn't set sensitivity_level explicitly
    lands on the server default ('internal'). Matters because most
    existing call sites (the team-from-README generator, the
    auto-ensure path in events) don't pass the field."""
    workspace_id = alice["workspace"]["id"]
    with session_scope() as s:
        _make_agent(s, workspace_id, name="argus")
    with session_scope() as s:
        a = s.execute(
            select(Agent).where(
                Agent.workspace_id == workspace_id,
                Agent.name == "argus",
            )
        ).scalar_one()
    assert a.sensitivity_level == DEFAULT_SENSITIVITY_LEVEL
    assert a.sensitivity_level == "internal"


def test_agent_can_be_inserted_at_any_valid_level(client, alice):
    """Round-trip: explicit level survives commit + reload."""
    workspace_id = alice["workspace"]["id"]
    with session_scope() as s:
        for i, level in enumerate(["public", "internal", "sensitive", "pii"]):
            _make_agent(
                s, workspace_id, name=f"bot-{i}",
                sensitivity_level=level,
            )

    with session_scope() as s:
        rows = s.execute(
            select(Agent.name, Agent.sensitivity_level)
            .where(Agent.workspace_id == workspace_id)
            .order_by(Agent.name)
        ).all()
    levels = {name: lvl for name, lvl in rows}
    assert levels == {
        "bot-0": "public",
        "bot-1": "internal",
        "bot-2": "sensitive",
        "bot-3": "pii",
    }


# ---------- Run: DB column behavior ---------- #


def test_run_defaults_to_internal_when_unspecified(client, alice):
    workspace_id = alice["workspace"]["id"]
    with session_scope() as s:
        _make_agent(s, workspace_id, name="argus")
        run_id = _make_run(s, workspace_id, agent_name="argus")
    with session_scope() as s:
        r = s.get(Run, run_id)
    assert r.sensitivity_level == "internal"


def test_run_can_carry_explicit_level(client, alice):
    """The 16.x-future run-create path snapshots the agent's level
    into the run row; confirm that snapshot survives a roundtrip."""
    workspace_id = alice["workspace"]["id"]
    with session_scope() as s:
        _make_agent(s, workspace_id, name="argus")
        run_id = _make_run(
            s, workspace_id, agent_name="argus",
            sensitivity_level="pii",
        )
    with session_scope() as s:
        r = s.get(Run, run_id)
    assert r.sensitivity_level == "pii"


# ---------- alembic backfill ---------- #


def test_existing_rows_landed_on_internal_after_migration(client, alice):
    """Both columns are NOT NULL so any row in the test DB (which
    was set up by the full alembic chain including 0027) must have a
    non-null level. Combined with the backfill defaulting to
    'internal', the existing test fixtures should all read 'internal'."""
    workspace_id = alice["workspace"]["id"]
    with session_scope() as s:
        _make_agent(s, workspace_id, name="argus")
        _make_run(s, workspace_id, agent_name="argus")

    with session_scope() as s:
        # Hit the DB directly to avoid the ORM layer applying any
        # client-side default — the assertion is about what's actually
        # stored.
        rows = s.execute(
            text(
                "SELECT sensitivity_level FROM agents "
                "WHERE workspace_id = :ws"
            ),
            {"ws": workspace_id},
        ).all()
    assert rows  # at least one agent
    assert all(lvl == "internal" for (lvl,) in rows)

    with session_scope() as s:
        rows = s.execute(
            text(
                "SELECT sensitivity_level FROM runs WHERE workspace_id = :ws"
            ),
            {"ws": workspace_id},
        ).all()
    assert rows
    assert all(lvl == "internal" for (lvl,) in rows)


def test_sensitivity_level_column_is_not_nullable(client, alice):
    """Belt-and-suspenders: the DB layer refuses NULLs. Documents the
    invariant — even if the model's default goes away, the column itself
    will reject a NULL write."""
    workspace_id = alice["workspace"]["id"]
    from sqlalchemy.exc import IntegrityError
    with session_scope() as s:
        with pytest.raises(IntegrityError):
            s.execute(
                text(
                    "INSERT INTO agents "
                    "(workspace_id, name, role, sensitivity_level, "
                    " created_at, updated_at) "
                    "VALUES (:ws, 'null-test', 'executor', NULL, "
                    " now(), now())"
                ),
                {"ws": workspace_id},
            )
