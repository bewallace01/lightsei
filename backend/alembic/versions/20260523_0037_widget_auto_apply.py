"""Phase 21.9: workspace setting for auto-applying Polaris's
suggested fixes to widget bot system prompts.

One boolean column on `workspaces`. Default false — the auto-apply
path is opt-in per CLAUDE.md's "operator-driven" defaults.

Revision ID: 0037
Revises: 0036
Create Date: 2026-05-23
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0037"
down_revision: Union[str, None] = "0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "polaris_auto_apply_widget_fixes",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "polaris_auto_apply_widget_fixes")
