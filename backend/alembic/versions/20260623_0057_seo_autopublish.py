"""Phase 37.10: workspaces.seo_autopublish_enabled + seo_autopublish_repo_id.

Spica auto-opens the publish PR. When a workspace opts in and picks a target
repo, a new SEO page draft (seo.page_drafted) auto-opens a publish PR. Both
columns are additive: enabled defaults false (opt-in), repo_id is nullable
(no target yet), so existing workspaces are unaffected and nothing publishes
without an explicit opt-in.

Revision ID: 0057
Revises: 0056
Create Date: 2026-06-23
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0057"
down_revision: Union[str, None] = "0056"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "seo_autopublish_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column("seo_autopublish_repo_id", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "seo_autopublish_repo_id")
    op.drop_column("workspaces", "seo_autopublish_enabled")
