"""Phase 32.11: feeder_settings (per-workspace feeder on/off).

Additive: a workspace with no row keeps the default (every feeder
enabled). A row appears only when the owner toggles a feeder off/on.

Revision ID: 0051
Revises: 0050
Create Date: 2026-06-09
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0051"
down_revision: Union[str, None] = "0050"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "feeder_settings",
        sa.Column(
            "workspace_id",
            sa.String(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("feeder_kind", sa.String(length=64), primary_key=True),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("feeder_settings")
