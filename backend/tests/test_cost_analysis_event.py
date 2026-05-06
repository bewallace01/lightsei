"""Phase 12D.2: `polaris.cost_analysis` event.

Two surfaces under test:
1. New workspaces are seeded with the schema_strict validator pack so
   `polaris.cost_analysis` ingests are validated by default.
2. The validator rejects empty / malformed insight payloads at
   /events ingest, before the row is written.
"""
from datetime import datetime, timezone

from db import session_scope
from models import ValidatorConfig
from tests.conftest import auth_headers


def _post_event(client, headers, *, payload):
    return client.post(
        "/events",
        json={
            "kind": "polaris.cost_analysis",
            "run_id": "run-cost-analysis-1",
            "agent_name": "polaris",
            "payload": payload,
        },
        headers=headers,
    )


def test_signup_seeds_cost_analysis_validator(client, alice):
    """Phase 12D.2 default validators land on every new workspace via
    `seed_default_validators`. Alice was created by the conftest's
    signup fixture; verify the row is there."""
    workspace_id = alice["workspace"]["id"]
    with session_scope() as s:
        row = s.get(
            ValidatorConfig,
            (workspace_id, "polaris.cost_analysis", "schema_strict"),
        )
        assert row is not None
        assert row.mode == "blocking"
        # Schema enforces a non-empty insights list.
        items_schema = row.config["schema"]["properties"]["insights"]
        assert items_schema.get("minItems") == 1


def test_cost_analysis_with_insights_passes(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    payload = {
        "insights": [
            {
                "kind": "model_tier_mismatch",
                "headline": "swap atlas to haiku",
                "detail": {"agent": "atlas"},
                "apply": None,
            },
        ],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": 30,
    }
    r = _post_event(client, h, payload=payload)
    assert r.status_code == 200, r.text


def test_cost_analysis_empty_insights_rejected(client, alice):
    """Defense in depth: Polaris filters before emit, but if a buggy
    bot sends an empty list anyway, the event is rejected upstream."""
    h = auth_headers(alice["api_key"]["plaintext"])
    payload = {
        "insights": [],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": 30,
    }
    r = _post_event(client, h, payload=payload)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    # Surface the failing validator + schema path so the bot's logs are
    # actionable.
    assert "schema_strict" in str(detail)


def test_cost_analysis_missing_required_field_rejected(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    payload = {
        # missing generated_at + window_days
        "insights": [
            {
                "kind": "k",
                "headline": "h",
                "detail": {},
            },
        ],
    }
    r = _post_event(client, h, payload=payload)
    assert r.status_code == 422, r.text
