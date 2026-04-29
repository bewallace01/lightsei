"""End-to-end tests for the Phase 7.3 validator pipeline.

Covers the API surface (PUT/GET/DELETE on /workspaces/me/validators)
plus the integration with POST /events: registered validators run
against the new event's payload, results land in event_validations,
unregistered validators don't run, cross-workspace isolation holds,
and a registry-mismatch (config references an unknown validator) is
recorded as status='error' rather than crashing the request.

Pure unit tests for validator functions live in test_validators.py.
"""
import uuid

from sqlalchemy import select

from models import EventValidation
from tests.conftest import auth_headers


_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "next_actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "blocked_by": {
                        "anyOf": [{"type": "string"}, {"type": "null"}]
                    },
                },
                "required": ["task", "blocked_by"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "next_actions"],
    "additionalProperties": False,
}


def _ok_plan_payload() -> dict:
    return {
        "summary": "all good",
        "next_actions": [{"task": "ship it", "blocked_by": None}],
    }


def _post_event(client, headers, run_id, agent, kind, payload):
    r = client.post(
        "/events",
        json={
            "run_id": run_id,
            "agent_name": agent,
            "kind": kind,
            "payload": payload,
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _register(client, headers, event_kind, validator_name, config):
    r = client.put(
        f"/workspaces/me/validators/{event_kind}/{validator_name}",
        json={"config": config},
        headers=headers,
    )
    return r


# ---------- endpoint CRUD ---------- #


def test_put_creates_then_lists_validator_config(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    r = _register(client, h, "polaris.plan", "schema_strict", {"schema": _PLAN_SCHEMA})
    assert r.status_code == 200
    body = r.json()
    assert body["event_kind"] == "polaris.plan"
    assert body["validator_name"] == "schema_strict"
    assert body["config"]["schema"]["required"] == ["summary", "next_actions"]

    listed = client.get("/workspaces/me/validators", headers=h).json()
    assert len(listed["validators"]) == 1
    assert listed["validators"][0]["validator_name"] == "schema_strict"


def test_put_is_idempotent_and_updates_config(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    _register(client, h, "polaris.plan", "schema_strict", {"schema": _PLAN_SCHEMA})
    new_schema = dict(_PLAN_SCHEMA, required=["summary"])  # narrower
    r = _register(client, h, "polaris.plan", "schema_strict", {"schema": new_schema})
    assert r.status_code == 200
    listed = client.get("/workspaces/me/validators", headers=h).json()
    assert len(listed["validators"]) == 1
    assert listed["validators"][0]["config"]["schema"]["required"] == ["summary"]


def test_put_rejects_unknown_validator_name(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    r = _register(client, h, "polaris.plan", "nonexistent", {})
    assert r.status_code == 400
    assert "registry" in r.json()["detail"]


def test_put_rejects_malformed_event_kind(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    # Uppercase not allowed by the regex.
    r = client.put(
        "/workspaces/me/validators/POLARIS_PLAN/schema_strict",
        json={"config": {"schema": _PLAN_SCHEMA}},
        headers=h,
    )
    assert r.status_code == 400


def test_put_rejects_malformed_validator_name(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    # Dots not allowed in validator_name (only event_kind).
    r = client.put(
        "/workspaces/me/validators/polaris.plan/schema.strict",
        json={"config": {"schema": _PLAN_SCHEMA}},
        headers=h,
    )
    assert r.status_code == 400


def test_delete_removes_config(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    _register(client, h, "polaris.plan", "schema_strict", {"schema": _PLAN_SCHEMA})

    r = client.delete(
        "/workspaces/me/validators/polaris.plan/schema_strict", headers=h
    )
    assert r.status_code == 200
    assert client.get("/workspaces/me/validators", headers=h).json() == {
        "validators": []
    }


def test_delete_404_when_not_registered(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    r = client.delete(
        "/workspaces/me/validators/polaris.plan/schema_strict", headers=h
    )
    assert r.status_code == 404


def test_validators_workspace_isolation(client, alice, bob):
    ha = auth_headers(alice["api_key"]["plaintext"])
    hb = auth_headers(bob["api_key"]["plaintext"])
    _register(client, ha, "polaris.plan", "schema_strict", {"schema": _PLAN_SCHEMA})

    # Bob doesn't see alice's registration.
    assert client.get("/workspaces/me/validators", headers=hb).json() == {
        "validators": []
    }
    # Bob can't delete it either.
    r = client.delete(
        "/workspaces/me/validators/polaris.plan/schema_strict", headers=hb
    )
    assert r.status_code == 404

    # Alice still sees her config intact.
    assert (
        len(client.get("/workspaces/me/validators", headers=ha).json()["validators"])
        == 1
    )


def test_unauthorized(client):
    assert client.get("/workspaces/me/validators").status_code == 401
    assert client.put(
        "/workspaces/me/validators/polaris.plan/schema_strict",
        json={"config": {}},
    ).status_code == 401


# ---------- pipeline integration with POST /events ---------- #


def _validations_for_event(client, headers, run_id):
    """Reach into the test DB through the events API + ORM."""
    # Pull events for the run
    r = client.get(f"/runs/{run_id}/events", headers=headers)
    assert r.status_code == 200
    return r.json()["events"]


def test_post_events_triggers_registered_validators(client, alice):
    """Happy path: register schema_strict on polaris.plan, post a clean
    event, expect a 'pass' validation row to land for it."""
    from db import engine
    from sqlalchemy.orm import Session as ORMSession

    h = auth_headers(alice["api_key"]["plaintext"])
    _register(client, h, "polaris.plan", "schema_strict", {"schema": _PLAN_SCHEMA})

    run_id = str(uuid.uuid4())
    _post_event(client, h, run_id, "polaris", "run_started", {})
    event_resp = _post_event(
        client, h, run_id, "polaris", "polaris.plan", _ok_plan_payload()
    )

    # event_validations row check (read directly — no API endpoint for this yet)
    with ORMSession(engine) as s:
        rows = s.execute(
            select(EventValidation).where(EventValidation.event_id == event_resp["id"])
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].validator_name == "schema_strict"
    assert rows[0].status == "pass"
    assert rows[0].violations == []


def test_post_events_with_invalid_payload_records_fail_row(client, alice):
    """Invalid payload -> validator emits violations -> status='fail'."""
    from db import engine
    from sqlalchemy.orm import Session as ORMSession

    h = auth_headers(alice["api_key"]["plaintext"])
    _register(client, h, "polaris.plan", "schema_strict", {"schema": _PLAN_SCHEMA})

    bad = _ok_plan_payload()
    bad["summary"] = 123  # wrong type

    run_id = str(uuid.uuid4())
    _post_event(client, h, run_id, "polaris", "run_started", {})
    ev = _post_event(client, h, run_id, "polaris", "polaris.plan", bad)

    with ORMSession(engine) as s:
        row = s.execute(
            select(EventValidation).where(EventValidation.event_id == ev["id"])
        ).scalar_one()
    assert row.status == "fail"
    assert any(v["rule"] == "type" for v in row.violations)


def test_post_events_with_no_registered_validators_runs_nothing(client, alice):
    """No validators registered for the kind -> no event_validations rows.
    The event still ingests normally."""
    from db import engine
    from sqlalchemy.orm import Session as ORMSession

    h = auth_headers(alice["api_key"]["plaintext"])
    run_id = str(uuid.uuid4())
    ev = _post_event(client, h, run_id, "polaris", "polaris.plan", _ok_plan_payload())

    with ORMSession(engine) as s:
        rows = s.execute(
            select(EventValidation).where(EventValidation.event_id == ev["id"])
        ).scalars().all()
    assert rows == []


def test_post_events_runs_multiple_validators(client, alice):
    """Both validators registered for the same kind both run."""
    from db import engine
    from sqlalchemy.orm import Session as ORMSession
    from validators.content_rules import DEFAULT_RULE_PACK

    h = auth_headers(alice["api_key"]["plaintext"])
    _register(client, h, "polaris.plan", "schema_strict", {"schema": _PLAN_SCHEMA})
    _register(client, h, "polaris.plan", "content_rules", {"rules": DEFAULT_RULE_PACK})

    run_id = str(uuid.uuid4())
    ev = _post_event(client, h, run_id, "polaris", "polaris.plan", _ok_plan_payload())

    with ORMSession(engine) as s:
        rows = s.execute(
            select(EventValidation).where(EventValidation.event_id == ev["id"])
            .order_by(EventValidation.validator_name)
        ).scalars().all()
    assert [r.validator_name for r in rows] == ["content_rules", "schema_strict"]
    assert all(r.status == "pass" for r in rows)


def test_post_events_handles_unknown_validator_name_in_db(client, alice, monkeypatch):
    """Stale config: someone removed a validator from REGISTRY but didn't
    delete the validator_configs row. Pipeline must record this without
    crashing the /events request."""
    from db import engine
    from sqlalchemy.orm import Session as ORMSession
    import validators

    h = auth_headers(alice["api_key"]["plaintext"])
    _register(client, h, "polaris.plan", "schema_strict", {"schema": _PLAN_SCHEMA})

    # Simulate: schema_strict gone from the registry mid-flight.
    original = dict(validators.REGISTRY)
    validators.REGISTRY.pop("schema_strict")
    try:
        run_id = str(uuid.uuid4())
        ev = _post_event(client, h, run_id, "polaris", "polaris.plan", _ok_plan_payload())

        with ORMSession(engine) as s:
            row = s.execute(
                select(EventValidation).where(EventValidation.event_id == ev["id"])
            ).scalar_one()
        assert row.status == "error"
        assert row.violations[0]["rule"] == "unknown_validator"
    finally:
        validators.REGISTRY.update(original)


def test_post_events_ingests_even_when_validator_crashes(client, alice, monkeypatch):
    """Defensive: a validator function raising an exception is recorded
    as status='error' and doesn't block ingestion."""
    from db import engine
    from sqlalchemy.orm import Session as ORMSession
    import validators

    h = auth_headers(alice["api_key"]["plaintext"])
    _register(client, h, "polaris.plan", "schema_strict", {"schema": _PLAN_SCHEMA})

    def boom(_payload, _config):
        raise RuntimeError("simulated validator bug")

    monkeypatch.setitem(validators.REGISTRY, "schema_strict", boom)

    run_id = str(uuid.uuid4())
    ev = _post_event(client, h, run_id, "polaris", "polaris.plan", _ok_plan_payload())

    with ORMSession(engine) as s:
        row = s.execute(
            select(EventValidation).where(EventValidation.event_id == ev["id"])
        ).scalar_one()
    assert row.status == "error"
    assert "simulated validator bug" in row.violations[0]["message"]


def test_pipeline_isolates_workspaces(client, alice, bob):
    """Alice's validator config doesn't run on bob's events."""
    from db import engine
    from sqlalchemy.orm import Session as ORMSession

    ha = auth_headers(alice["api_key"]["plaintext"])
    hb = auth_headers(bob["api_key"]["plaintext"])
    _register(client, ha, "polaris.plan", "schema_strict", {"schema": _PLAN_SCHEMA})

    bob_run = str(uuid.uuid4())
    bob_ev = _post_event(
        client, hb, bob_run, "polaris", "polaris.plan",
        {"intentionally": "wrong shape"},
    )
    with ORMSession(engine) as s:
        rows = s.execute(
            select(EventValidation).where(EventValidation.event_id == bob_ev["id"])
        ).scalars().all()
    assert rows == []


# ---------- Phase 8.1: validator mode (advisory | blocking) ---------- #


def _register_with_mode(client, headers, kind, name, config, mode):
    return client.put(
        f"/workspaces/me/validators/{kind}/{name}",
        json={"config": config, "mode": mode},
        headers=headers,
    )


def test_put_defaults_mode_to_advisory(client, alice):
    """Phase 7A clients omit `mode` entirely. They land as advisory —
    the existing post-emit-tag behavior."""
    h = auth_headers(alice["api_key"]["plaintext"])
    r = client.put(
        "/workspaces/me/validators/polaris.plan/schema_strict",
        json={"config": {"schema": _PLAN_SCHEMA}},  # no `mode`
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["mode"] == "advisory"


def test_put_round_trips_mode_blocking(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    r = _register_with_mode(
        client, h, "polaris.plan", "schema_strict",
        {"schema": _PLAN_SCHEMA}, "blocking",
    )
    assert r.status_code == 200
    assert r.json()["mode"] == "blocking"

    # And the GET listing reports the same.
    listed = client.get("/workspaces/me/validators", headers=h).json()
    assert listed["validators"][0]["mode"] == "blocking"


def test_put_round_trips_mode_advisory_explicit(client, alice):
    """Same as the default path, but with the value passed explicitly —
    pins the contract for callers that want to set the mode every time."""
    h = auth_headers(alice["api_key"]["plaintext"])
    r = _register_with_mode(
        client, h, "polaris.plan", "schema_strict",
        {"schema": _PLAN_SCHEMA}, "advisory",
    )
    assert r.status_code == 200
    assert r.json()["mode"] == "advisory"


def test_put_rejects_unknown_mode(client, alice):
    """Anything outside {advisory, blocking} 400s — keeps the API
    surface tight so a typo doesn't silently land an unintended
    behavior."""
    h = auth_headers(alice["api_key"]["plaintext"])
    r = _register_with_mode(
        client, h, "polaris.plan", "schema_strict",
        {"schema": _PLAN_SCHEMA}, "shadow",
    )
    assert r.status_code == 400
    assert "mode must be one of" in r.json()["detail"]


def test_put_can_promote_existing_advisory_to_blocking(client, alice):
    """Operator-flow regression: register a validator in advisory mode,
    observe Phase 7A behavior, then PUT again with mode=blocking to
    promote. The same row updates rather than a new row appearing."""
    h = auth_headers(alice["api_key"]["plaintext"])
    _register_with_mode(
        client, h, "polaris.plan", "schema_strict",
        {"schema": _PLAN_SCHEMA}, "advisory",
    )
    _register_with_mode(
        client, h, "polaris.plan", "schema_strict",
        {"schema": _PLAN_SCHEMA}, "blocking",
    )
    listed = client.get("/workspaces/me/validators", headers=h).json()
    assert len(listed["validators"]) == 1
    assert listed["validators"][0]["mode"] == "blocking"
