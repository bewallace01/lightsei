"""polaris.cost_analysis schema_strict validator (Phase 12D.2)

Defense-in-depth: rejects `polaris.cost_analysis` events that have an
empty / missing `insights` list at /events ingest. Polaris's tick loop
already filters before emit, so this row's job is to stop a buggy bot
from spamming. Seeds for every existing workspace; new workspaces get
the same row inline from `signup` / `POST /workspaces`.

Revision ID: 0024
Revises: 0023
Create Date: 2026-05-06
"""
import json
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


COST_ANALYSIS_SCHEMA = {
    "schema": {
        "type": "object",
        "required": ["insights", "generated_at", "window_days"],
        "properties": {
            "insights": {
                "type": "array",
                # The whole point of this validator: an empty list is a
                # bug, not a normal state. Polaris suppresses the emit
                # upstream when the filter result is empty.
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


def upgrade() -> None:
    now = datetime.now(timezone.utc)
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            INSERT INTO validator_configs
              (workspace_id, event_kind, validator_name, config, mode,
               created_at, updated_at)
            SELECT
              w.id,
              'polaris.cost_analysis',
              'schema_strict',
              CAST(:config AS jsonb),
              'blocking',
              :now,
              :now
            FROM workspaces w
            ON CONFLICT (workspace_id, event_kind, validator_name) DO NOTHING
            """
        ),
        {"config": json.dumps(COST_ANALYSIS_SCHEMA), "now": now},
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM validator_configs
         WHERE event_kind = 'polaris.cost_analysis'
           AND validator_name = 'schema_strict'
        """
    )
