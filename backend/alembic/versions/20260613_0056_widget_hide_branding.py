"""Phase 36.2: workspaces.widget_hide_branding.

Owner preference to hide the "Powered by Lightspace Labs" badge. Stored
freely; only honored on paid plans (the config endpoint gates it), so a
downgrade re-shows the badge automatically.

Revision ID: 0056
Revises: 0055
Create Date: 2026-06-13
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0056"
down_revision: Union[str, None] = "0055"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "widget_hide_branding",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "widget_hide_branding")
