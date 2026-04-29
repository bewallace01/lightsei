"""validator_mode: advisory | blocking per validator config

Phase 8.1 of the output-validation rollout. Adds a `mode` column to
`validator_configs` so operators can opt individual configs into
"blocking" — payloads failing such a validator are rejected at
ingestion (POST /events returns 422). Default `advisory` keeps every
existing row Phase-7A behavior; nothing breaks on upgrade.

Phase 8.2 wires the pipeline to honor the mode.

Revision ID: 0014
Revises: 0013
Create Date: 2026-04-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # NOT NULL with a server default lets existing rows backfill atomically.
    # The default also covers the API path: a PUT that omits `mode` lands
    # advisory, matching Phase 7A's implicit behavior.
    op.add_column(
        "validator_configs",
        sa.Column(
            "mode",
            sa.String(length=16),
            nullable=False,
            server_default="advisory",
        ),
    )


def downgrade() -> None:
    op.drop_column("validator_configs", "mode")
