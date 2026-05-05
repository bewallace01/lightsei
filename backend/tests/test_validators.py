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


# ---------- content-rules: happy + sad paths ---------- #


from validators import content_rules
from validators.content_rules import (
    DEFAULT_RULE_PACK,
    _redact_match,
    validate as content_rules_validate,
)


def _polaris_like_payload(summary="all good", task="ship it") -> dict:
    return {
        "summary": summary,
        "next_actions": [{"task": task, "blocked_by": None}],
    }


def test_content_rules_passes_clean_payload():
    result = content_rules_validate(
        _polaris_like_payload(),
        {"rules": DEFAULT_RULE_PACK},
    )
    assert result == {"ok": True, "violations": []}


def test_content_rules_flags_email_in_summary():
    payload = _polaris_like_payload(
        summary="send the report to alice@example.com next week"
    )
    result = content_rules_validate(payload, {"rules": DEFAULT_RULE_PACK})
    assert result["ok"] is False
    assert len(result["violations"]) == 1
    v = result["violations"][0]
    assert v["rule"] == "email_in_summary"
    assert v["path"] == "summary"
    assert v["severity"] == "fail"
    # Long match (the email) is redacted; full email is not stored on the
    # violation. The original event payload still has it for audit.
    assert "@example.com" not in v["matched"]
    assert v["matched"].endswith("***")


def test_content_rules_flags_destructive_verb():
    payload = _polaris_like_payload(task="delete the orphaned cache rows")
    result = content_rules_validate(payload, {"rules": DEFAULT_RULE_PACK})
    assert result["ok"] is False
    assert len(result["violations"]) == 1
    v = result["violations"][0]
    assert v["rule"] == "banned_destructive_verbs"
    # "delete" is short (6 chars), kept verbatim so the operator can see
    # which verb fired without going to the original event.
    assert v["matched"] == "delete"


def test_content_rules_emits_one_violation_per_array_match():
    """Two destructive verbs in two next_actions yield two violations,
    not one merged one."""
    payload = {
        "summary": "tidy up",
        "next_actions": [
            {"task": "delete this file", "blocked_by": None},
            {"task": "destroy the cache", "blocked_by": None},
            {"task": "leave alone", "blocked_by": None},
        ],
    }
    result = content_rules_validate(payload, {"rules": DEFAULT_RULE_PACK})
    assert result["ok"] is False
    assert len(result["violations"]) == 2
    matched = sorted(v["matched"] for v in result["violations"])
    assert matched == ["delete", "destroy"]


def test_content_rules_does_not_flag_drop_as_destructive():
    """Regression: `drop` was in the banned-verbs list originally and
    produced too many false positives on normal English usage. Plans
    that say "drop a file on /agents/new" or "drop the parking-lot
    item" should pass; the rule still catches the unambiguously
    destructive verbs (delete / truncate / destroy / nuke)."""
    payload = _polaris_like_payload(task="drop a zip on the dashboard")
    result = content_rules_validate(payload, {"rules": DEFAULT_RULE_PACK})
    assert result["ok"] is True
    assert result["violations"] == []


def test_content_rules_must_match_mode_fails_on_missing_pattern():
    rule = {
        "name": "must_have_summary_text",
        "pattern": r"\w+",
        "fields": ["summary"],
        "mode": "must_match",
        "severity": "fail",
    }
    result = content_rules_validate({"summary": ""}, {"rules": [rule]})
    assert result["ok"] is False
    v = result["violations"][0]
    assert v["rule"] == "must_have_summary_text"
    assert "did not match" in v["message"]


def test_content_rules_warn_severity_keeps_ok_true():
    """A warn-severity violation is recorded but doesn't fail the result.
    This is the hook for advisory-only rules — the dashboard chips show
    WARN, but the event is still considered valid."""
    rule = {
        "name": "soft_warning",
        "pattern": r"todo",
        "fields": ["summary"],
        "mode": "must_not_match",
        "severity": "warn",
    }
    result = content_rules_validate(
        {"summary": "todo: fix this later"},
        {"rules": [rule]},
    )
    assert result["ok"] is True
    assert len(result["violations"]) == 1
    assert result["violations"][0]["severity"] == "warn"


def test_content_rules_invalid_regex_reported_per_rule():
    """A bad rule doesn't crash the whole pipeline — it emits its own
    violation and other rules still run."""
    rules = [
        {
            "name": "bad_pattern",
            "pattern": "[unclosed",
            "fields": ["summary"],
            "mode": "must_not_match",
            "severity": "fail",
        },
        DEFAULT_RULE_PACK[0],  # email_in_summary, should still run
    ]
    payload = _polaris_like_payload(summary="contact alice@example.com")
    result = content_rules_validate(payload, {"rules": rules})
    rule_names = sorted(v["rule"] for v in result["violations"])
    assert rule_names == ["bad_pattern", "email_in_summary"]


def test_content_rules_missing_pattern_reported():
    rule = {"name": "no_pattern", "fields": ["summary"]}
    result = content_rules_validate({"summary": "x"}, {"rules": [rule]})
    assert result["ok"] is False
    assert result["violations"][0]["rule"] == "no_pattern"
    assert "missing 'pattern'" in result["violations"][0]["message"]


def test_content_rules_missing_config_returns_violation():
    """No rules in config -> validator reports it instead of silently
    passing every payload."""
    result = content_rules_validate({"summary": "x"}, {})
    assert result["ok"] is False
    assert result["violations"][0]["rule"] == "missing_config"


def test_content_rules_missing_field_silently_yields_nothing():
    """Schema-strict catches missing fields; content-rules treats them as
    'no values to check' so a single payload going through both validators
    doesn't double-report the same problem."""
    rule = {
        "name": "summary_no_email",
        "pattern": r"@",
        "fields": ["summary"],
        "mode": "must_not_match",
        "severity": "fail",
    }
    # No `summary` key at all — content-rules should pass, schema-strict
    # would have caught the missing required field separately.
    result = content_rules_validate({"other_field": "x"}, {"rules": [rule]})
    assert result == {"ok": True, "violations": []}


def test_content_rules_array_path_resolution():
    """Verify the [] path syntax works on nested arrays."""
    payload = {
        "items": [
            {"name": "alice"},
            {"name": "bob"},
            {"name": "DESTROY ALL"},
        ],
    }
    rule = {
        "name": "no_caps_yelling",
        "pattern": r"[A-Z]{3,}",
        "fields": ["items[].name"],
        "mode": "must_not_match",
        "severity": "fail",
    }
    result = content_rules_validate(payload, {"rules": [rule]})
    assert result["ok"] is False
    assert len(result["violations"]) == 1


def test_content_rules_runs_via_registry():
    payload = _polaris_like_payload()
    result = run_validator(
        "content_rules", payload, {"rules": DEFAULT_RULE_PACK}
    )
    assert result["ok"] is True


def test_default_rule_pack_carries_canonical_rules():
    """Demo and dashboard test fixtures depend on these names. If they
    rename, the demo and the screenshots' alt text need updating too."""
    rule_names = {r["name"] for r in DEFAULT_RULE_PACK}
    assert rule_names == {"email_in_summary", "banned_destructive_verbs"}


def test_default_rule_pack_is_case_insensitive():
    """`Delete` and `Alice@Example.COM` should fire the same as their
    lowercase forms. Surfaced during the 7.5 dashboard demo: a Polaris
    plan saying 'Delete the cache' didn't get flagged because the
    default regex was case-sensitive. Locking in the (?i) prefix here."""
    payload = {
        "summary": "ping Alice@EXAMPLE.com if a deploy needs sign-off",
        "next_actions": [
            {"task": "Delete the orphaned cache rows", "blocked_by": None},
            {"task": "TRUNCATE TABLE staging_runs", "blocked_by": None},
        ],
    }
    result = content_rules_validate(payload, {"rules": DEFAULT_RULE_PACK})
    assert result["ok"] is False
    rule_names = sorted(v["rule"] for v in result["violations"])
    assert rule_names == [
        "banned_destructive_verbs",
        "banned_destructive_verbs",
        "email_in_summary",
    ]


def test_redact_match_threshold():
    # Short keywords kept verbatim
    assert _redact_match("delete") == "delete"   # 6 chars
    assert _redact_match("destroy") == "destroy"  # 7 chars
    # Boundary: 8 chars triggers redaction
    assert _redact_match("destroyer") == "d***"  # 9 chars
    # Real-world PII-shaped match
    assert _redact_match("alice@example.com") == "a***"
    # Tiny inputs kept (no point redacting one char)
    assert _redact_match("a") == "a"


def test_registry_contains_content_rules():
    assert "content_rules" in REGISTRY
