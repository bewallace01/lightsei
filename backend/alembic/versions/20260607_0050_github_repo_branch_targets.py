"""Phase 10B.4: github_repo_branch_targets (per-env branch -> agent).

Maps a repo's branch to the agents that deploy when that branch is
pushed. Additive: a repo with no rows keeps the legacy single-branch
behavior.

Revision ID: 0050
Revises: 0049
Create Date: 2026-06-07
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0050"
down_revision: Union[str, None] = "0049"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "github_repo_branch_targets",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "repo_id",
            sa.String(),
            sa.ForeignKey("github_repos.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            sa.String(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("branch", sa.String(length=255), nullable=False),
        sa.Column("agent_name", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "uq_github_branch_targets_repo_branch_agent",
        "github_repo_branch_targets",
        ["repo_id", "branch", "agent_name"],
        unique=True,
    )
    op.create_index(
        "ix_github_branch_targets_repo_branch",
        "github_repo_branch_targets",
        ["repo_id", "branch"],
    )


def downgrade() -> None:
    op.drop_index("ix_github_branch_targets_repo_branch", table_name="github_repo_branch_targets")
    op.drop_index("uq_github_branch_targets_repo_branch_agent", table_name="github_repo_branch_targets")
    op.drop_table("github_repo_branch_targets")
