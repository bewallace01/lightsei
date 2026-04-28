"""Tests for backend/validators (Phase 7.1).

Schema-strict validator: covers clean pass, missing required field,
wrong type, additional property, multiple violations collected at once,
and the two config-error paths (missing schema, malformed schema).

The full validation pipeline (event_validations table, post-emit
hooks) lands in Phase 7.3 and has its own tests.
"""
import pytest

from validators import REGISTRY
from validators import validate as run_validator
from validators.schema_strict import validate as schema_strict_validate


# A Polaris-plan-ish schema, trimmed to what these tests need to exercise.
# The real polaris.plan schema lives in polaris/bot.py; the registration
# story is Phase 7.3, where Polaris registers its schema as the validator
# config on the workspace.
_POLARIS_LIKE_SCHEMA = {
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


def _ok_payload() -> dict:
    return {
        "summary": "all good",
        "next_actions": [{"task": "do x", "blocked_by": None}],
    }


# ---------- schema-strict: happy + sad paths ---------- #


def test_passes_on_clean_payload():
    result = schema_strict_validate(
        _ok_payload(), {"schema": _POLARIS_LIKE_SCHEMA}
    )
    assert result == {"ok": True, "violations": []}


def test_missing_required_field():
    payload = {"next_actions": []}  # summary missing
    result = schema_strict_validate(
        payload, {"schema": _POLARIS_LIKE_SCHEMA}
    )
    assert result["ok"] is False
    assert len(result["violations"]) == 1
    v = result["violations"][0]
    assert v["rule"] == "required"
    assert "summary" in v["message"]


def test_wrong_type():
    payload = _ok_payload()
    payload["summary"] = 123  # should be string
    result = schema_strict_validate(
        payload, {"schema": _POLARIS_LIKE_SCHEMA}
    )
    assert result["ok"] is False
    assert len(result["violations"]) == 1
    v = result["violations"][0]
    assert v["rule"] == "type"
    assert v["path"] == "/summary"


def test_additional_property():
    payload = _ok_payload()
    payload["bonus_field"] = "not allowed"
    result = schema_strict_validate(
        payload, {"schema": _POLARIS_LIKE_SCHEMA}
    )
    assert result["ok"] is False
    # exactly one additionalProperties violation, located at object root
    assert len(result["violations"]) == 1
    v = result["violations"][0]
    assert v["rule"] == "additionalProperties"


def test_collects_multiple_violations_in_one_pass():
    """Wrong type on summary AND a missing-required deep in next_actions —
    both should be reported, not just whichever the validator hits first."""
    payload = {
        "summary": 123,
        "next_actions": [{"task": "x"}],  # blocked_by missing
    }
    result = schema_strict_validate(
        payload, {"schema": _POLARIS_LIKE_SCHEMA}
    )
    assert result["ok"] is False
    rules = sorted(v["rule"] for v in result["violations"])
    assert rules == ["required", "type"]


def test_path_is_a_json_pointer_for_nested_violations():
    payload = _ok_payload()
    payload["next_actions"][0]["task"] = 42  # wrong type, deep
    result = schema_strict_validate(
        payload, {"schema": _POLARIS_LIKE_SCHEMA}
    )
    assert result["ok"] is False
    assert result["violations"][0]["path"] == "/next_actions/0/task"


# ---------- schema-strict: config-error paths ---------- #


def test_empty_config_returns_missing_config_violation():
    """No schema in config -> validator reports it instead of crashing.
    Pipeline-friendly: a mis-registered config doesn't kill ingestion."""
    result = schema_strict_validate({"x": 1}, {})
    assert result["ok"] is False
    assert result["violations"][0]["rule"] == "missing_config"


def test_invalid_schema_in_config_returns_invalid_schema_violation():
    bad_schema = {"type": "not_a_real_type"}
    result = schema_strict_validate({"x": 1}, {"schema": bad_schema})
    assert result["ok"] is False
    assert result["violations"][0]["rule"] == "invalid_schema"


# ---------- registry ---------- #


def test_runs_via_registry():
    """Public validate(name, ...) entry point routes by name."""
    result = run_validator(
        "schema_strict",
        _ok_payload(),
        {"schema": _POLARIS_LIKE_SCHEMA},
    )
    assert result["ok"] is True


def test_registry_lookup_for_unknown_validator_raises_keyerror():
    with pytest.raises(KeyError, match="nonexistent"):
        run_validator("nonexistent", {}, {})


def test_registry_contains_schema_strict():
    """Future-proofing: if the registry key is renamed, the rename
    must also touch any persisted event_validations.validator_name
    rows. This test pins the canonical name."""
    assert "schema_strict" in REGISTRY
