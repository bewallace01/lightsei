"""Phase 16.2: tests for the capability model + PATCH endpoint.

Three surfaces:

1. Pure module (`backend/capabilities.py`): is_valid_capability,
   validate_capability_list (the validator the endpoint returns
   problems from), normalize_capability_list, presets_for_level.
2. Schema default: a freshly-inserted Agent row lands on `[]`.
3. PATCH endpoint contract: 200 on valid, 422 with `problems` list on
   invalid, 404 on unknown agent, cross-workspace 404 (Bob's PATCH
   against Alice's agent doesn't 200 silently).

The actual SDK enforcement that uses this column is Phase 16.3 and
gets its own test file. This sub-task is just storage + validation.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

import capabilities as caps
from db import session_scope
from models import (
    DEFAULT_SENSITIVITY_LEVEL,
    Agent,
)
from tests.conftest import auth_headers


# ---------- Helpers ---------- #


def _make_agent(session, workspace_id, *, name, **kwargs):
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


# ---------- is_valid_capability ---------- #


def test_known_capabilities_are_accepted():
    for name in caps.KNOWN_CAPABILITIES:
        assert caps.is_valid_capability(name), name


def test_connector_prefix_accepts_arbitrary_suffixes():
    """`connector:<name>` is a forward-compat prefix — workspaces can
    set it today even though Phase 20 hasn't wired enforcement yet."""
    for ok in ("connector:hubspot", "connector:slack", "connector:gmail",
               "connector:my_custom_thing", "connector:foo-bar"):
        assert caps.is_valid_capability(ok), ok


def test_connector_prefix_rejects_empty_or_whitespace_suffix():
    """`connector:` alone is meaningless; `connector: ` with whitespace
    would slip past a naive startswith check."""
    for bad in ("connector:", "connector: ", "connector:  foo", "connector:foo!"):
        assert not caps.is_valid_capability(bad), bad


def test_unknown_capability_without_prefix_is_rejected():
    """Random strings don't get through. Forces the user to either
    pick from the known set or use the connector: prefix explicitly."""
    for bad in ("filesystem", "shell", "outbound_http", "INTERNET", " internet "):
        assert not caps.is_valid_capability(bad), bad


def test_non_string_inputs_are_rejected():
    for bad in (None, 0, [], {}, ["internet"]):
        assert not caps.is_valid_capability(bad), repr(bad)


def test_capability_length_cap():
    """Defense against a misconfigured caller storing a multi-KB string
    as a capability name."""
    huge = "connector:" + ("a" * 200)
    assert not caps.is_valid_capability(huge)


def test_known_capabilities_is_frozen():
    """The known set isn't mutable at runtime — keeps the vocabulary
    auditable."""
    assert isinstance(caps.KNOWN_CAPABILITIES, frozenset)


# ---------- validate_capability_list ---------- #


def test_empty_list_is_valid():
    """Empty allow-list = the default-deny state; no problems."""
    assert caps.validate_capability_list([]) == []


def test_all_known_capabilities_validate_clean():
    assert caps.validate_capability_list(
        list(caps.KNOWN_CAPABILITIES) + ["connector:hubspot"]
    ) == []


def test_validator_rejects_non_list_input():
    """The PATCH endpoint relies on this returning a problems list (not
    raising) so it can render line errors on the dashboard."""
    problems = caps.validate_capability_list("internet")  # type: ignore[arg-type]
    assert len(problems) == 1
    assert "must be a list" in problems[0]


def test_validator_flags_unknown_capability_with_index():
    problems = caps.validate_capability_list(
        ["internet", "filesystem", "send_command"]
    )
    assert len(problems) == 1
    assert "capabilities[1]" in problems[0]
    assert "filesystem" in problems[0]


def test_validator_flags_duplicates():
    """A duplicate at index N is flagged; the first occurrence is fine."""
    problems = caps.validate_capability_list(
        ["internet", "send_command", "internet"]
    )
    assert len(problems) == 1
    assert "capabilities[2]" in problems[0]
    assert "duplicate" in problems[0]


def test_validator_enforces_per_agent_cap():
    """50 distinct capabilities is the cap; 51 trips the limit."""
    too_many = [f"connector:c{i}" for i in range(60)]
    problems = caps.validate_capability_list(too_many)
    assert any("cap is 50" in p for p in problems)


def test_validator_aggregates_multiple_problems():
    """All problems surface so the user can fix the whole list in one
    pass rather than one error at a time."""
    problems = caps.validate_capability_list(
        ["internet", "filesystem", "FILESYSTEM", "internet"]
    )
    # filesystem (unknown), FILESYSTEM (unknown), internet (dup) — 3 total.
    assert len(problems) == 3


# ---------- normalize_capability_list ---------- #


def test_normalize_dedups_preserving_order():
    """First occurrence wins; later duplicates are dropped silently
    after validation has passed."""
    out = caps.normalize_capability_list(
        ["internet", "send_command", "internet", "connector:hubspot"]
    )
    assert out == ["internet", "send_command", "connector:hubspot"]


# ---------- presets_for_level ---------- #


def test_presets_default_deny_for_sensitive_and_pii():
    """The canonical CRM-bot scenario: a `'pii'` agent starts with
    zero capabilities. Every grant must be an explicit user choice."""
    assert caps.presets_for_level("pii") == []
    assert caps.presets_for_level("sensitive") == []


def test_presets_public_gets_open_research_set():
    """`'public'` agents are the canonical 'open research bot' role
    — internet + send_command by default so the user doesn't have to
    configure either."""
    p = caps.presets_for_level("public")
    assert "internet" in p
    assert "send_command" in p


def test_presets_internal_is_middle_ground():
    """`'internal'` gets send_command (the standard executor pattern)
    but not internet — the default sensible posture for a workspace
    that hasn't thought about zones."""
    p = caps.presets_for_level("internal")
    assert "send_command" in p
    assert "internet" not in p


def test_presets_unknown_level_falls_back_to_default():
    """Garbage in shouldn't crash. Falls back to the default
    sensitivity level's preset so a None or off-list value at a call
    site doesn't need a separate is_valid check upstream."""
    assert caps.presets_for_level(None) == caps.presets_for_level(
        DEFAULT_SENSITIVITY_LEVEL
    )
    assert caps.presets_for_level("bogus") == caps.presets_for_level(
        DEFAULT_SENSITIVITY_LEVEL
    )


def test_presets_returns_a_fresh_list_each_call():
    """Returns a copy so callers can mutate without poisoning the
    next call's default."""
    a = caps.presets_for_level("public")
    a.append("filesystem")
    b = caps.presets_for_level("public")
    assert "filesystem" not in b


# ---------- Agent schema default ---------- #


def test_agent_defaults_to_empty_capabilities(client, alice):
    """A fresh agent row that doesn't specify capabilities lands on
    `[]` — default-deny is the safe posture for a new bot."""
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
    assert a.capabilities == []
    # Also confirm the sensitivity level default (from 16.1) is intact —
    # the new column added in 0028 shouldn't disturb the 0027 default.
    assert a.sensitivity_level == DEFAULT_SENSITIVITY_LEVEL


# ---------- PATCH endpoint ---------- #


def test_patch_capabilities_replaces_list(client, alice):
    workspace_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    with session_scope() as s:
        _make_agent(s, workspace_id, name="argus")

    r = client.patch(
        "/agents/argus/capabilities",
        headers=auth_headers(api_key),
        json={"capabilities": ["internet", "connector:slack"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "argus"
    assert body["capabilities"] == ["internet", "connector:slack"]


def test_patch_capabilities_422_with_problems_on_invalid_entry(client, alice):
    workspace_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    with session_scope() as s:
        _make_agent(s, workspace_id, name="argus")

    r = client.patch(
        "/agents/argus/capabilities",
        headers=auth_headers(api_key),
        json={"capabilities": ["internet", "filesystem"]},
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    # Endpoint wraps in {"problems": [...]} so the dashboard can render
    # line-level errors.
    assert "problems" in detail
    assert any("filesystem" in p for p in detail["problems"])


def test_patch_capabilities_dedups_before_persisting(client, alice):
    """Two-entry input where both entries are the same → one stored.
    The validator caught it as a duplicate-problem too, but if a future
    callsite skips that check (e.g. an SDK helper), normalize_capability_list
    in the endpoint still tidies the persisted list."""
    workspace_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    with session_scope() as s:
        _make_agent(s, workspace_id, name="argus")

    # First a clean PATCH so the row exists with a sane state.
    r = client.patch(
        "/agents/argus/capabilities",
        headers=auth_headers(api_key),
        json={"capabilities": ["internet"]},
    )
    assert r.status_code == 200

    # Now confirm duplicate-input is 422 (validator path).
    r = client.patch(
        "/agents/argus/capabilities",
        headers=auth_headers(api_key),
        json={"capabilities": ["internet", "internet"]},
    )
    assert r.status_code == 422


def test_patch_capabilities_404_on_unknown_agent(client, alice):
    api_key = alice["api_key"]["plaintext"]
    r = client.patch(
        "/agents/does-not-exist/capabilities",
        headers=auth_headers(api_key),
        json={"capabilities": []},
    )
    assert r.status_code == 404


def test_patch_capabilities_cross_workspace_404(client, alice, bob):
    """Bob PATCHing Alice's agent must 404 — not silently update."""
    a_ws = alice["workspace"]["id"]
    with session_scope() as s:
        _make_agent(s, a_ws, name="argus")

    r = client.patch(
        "/agents/argus/capabilities",
        headers=auth_headers(bob["api_key"]["plaintext"]),
        json={"capabilities": ["internet"]},
    )
    assert r.status_code == 404

    # And Alice's row is unchanged.
    with session_scope() as s:
        a = s.execute(
            select(Agent).where(
                Agent.workspace_id == a_ws, Agent.name == "argus",
            )
        ).scalar_one()
    assert a.capabilities == []


def test_patch_capabilities_unauthenticated(client):
    r = client.patch(
        "/agents/argus/capabilities",
        json={"capabilities": []},
    )
    assert r.status_code == 401


def test_patch_capabilities_clears_to_empty_list(client, alice):
    """Sending `[]` is a valid revocation — strips an agent's
    capabilities back to default-deny without deleting the row."""
    workspace_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    with session_scope() as s:
        _make_agent(s, workspace_id, name="argus", capabilities=["internet"])

    r = client.patch(
        "/agents/argus/capabilities",
        headers=auth_headers(api_key),
        json={"capabilities": []},
    )
    assert r.status_code == 200
    assert r.json()["capabilities"] == []


def test_get_agent_serializes_capabilities(client, alice):
    """The GET /agents/{name} response must include the new field so
    the dashboard can render the capability list inline."""
    workspace_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    with session_scope() as s:
        _make_agent(
            s, workspace_id, name="argus",
            capabilities=["internet", "send_command"],
        )

    r = client.get("/agents/argus", headers=auth_headers(api_key))
    assert r.status_code == 200
    body = r.json()
    assert body["capabilities"] == ["internet", "send_command"]
    assert body["sensitivity_level"] == DEFAULT_SENSITIVITY_LEVEL
