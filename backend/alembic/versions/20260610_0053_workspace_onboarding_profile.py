"""Phase 33.1: workspaces.onboarding_profile.

Stores the business-onboarding answers ({industry, goals, completed_at}).
Nullable: absent = onboarding not done, which the dashboard uses to decide
whether to show the welcome wizard.

Revision ID: 0053
Revises: 0052
Create Date: 2026-06-10
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0053"
down_revision: Union[str, None] = "0052"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "onboarding_profile",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "onboarding_profile")
