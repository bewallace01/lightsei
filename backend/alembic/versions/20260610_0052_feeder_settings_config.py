"""Phase 32.15: feeder_settings.config (per-workspace feeder targeting).

Additive: a JSONB blob of feeder-specific config (e.g. which Google
Business Profile account + location the Reputation feeder polls). Absent /
empty means "auto-discover", preserving existing behavior with no backfill.

Revision ID: 0052
Revises: 0051
Create Date: 2026-06-10
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0052"
down_revision: Union[str, None] = "0051"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "feeder_settings",
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("feeder_settings", "config")
