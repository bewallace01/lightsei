"""Defaults for `validator_configs`, seeded on workspace creation.

Each entry maps `(event_kind, validator_name)` to its config + mode.
Migrations seed the same set for *existing* workspaces; this module
covers *new* workspaces (signup, POST /workspaces).

Schemas here are duplicated in the corresponding migration on purpose:
the migration is a frozen snapshot, this module evolves with the app.
When you change a schema here, write a migration to update existing
workspaces too.
"""
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from models import ValidatorConfig


# Phase 12D.2 — defense-in-depth on `polaris.cost_analysis`. Polaris's
# tick loop suppresses the emit upstream when the insight list is
# empty, so this validator's only job is to catch a buggy bot.
_POLARIS_COST_ANALYSIS_SCHEMA: dict[str, Any] = {
    "schema": {
        "type": "object",
        "required": ["insights", "generated_at", "window_days"],
        "properties": {
            "insights": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["kind", "headline", "detail"],
                    "properties": {
                        "kind": {"type": "string"},
                        "headline": {"type": "string"},
                        "detail": {"type": "object"},
                        "apply": {
                            "anyOf": [
                                {"type": "object"},
                                {"type": "null"},
                            ],
                        },
                    },
                },
            },
            "generated_at": {"type": "string"},
            "window_days": {"type": "integer"},
        },
    },
}


# (event_kind, validator_name) -> (config, mode)
DEFAULT_VALIDATORS: list[tuple[str, str, dict[str, Any], str]] = [
    (
        "polaris.cost_analysis",
        "schema_strict",
        _POLARIS_COST_ANALYSIS_SCHEMA,
        "blocking",
    ),
]


def seed_default_validators(
    session: Session, workspace_id: str, now: datetime
) -> None:
    """Insert the default validator rows for a freshly-created workspace.
    Idempotent: skips rows that already exist (signup is the only caller
    today, but if signup is ever retried, no duplicate-key crash)."""
    for event_kind, validator_name, config, mode in DEFAULT_VALIDATORS:
        existing = session.get(
            ValidatorConfig, (workspace_id, event_kind, validator_name)
        )
        if existing is not None:
            continue
        session.add(
            ValidatorConfig(
                workspace_id=workspace_id,
                event_kind=event_kind,
                validator_name=validator_name,
                config=config,
                mode=mode,
                created_at=now,
                updated_at=now,
            )
        )
