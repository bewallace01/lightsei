"""deployments.source + deployments.source_commit_sha (Phase 10.3)

Two additive columns on `deployments`:

  source            'cli' | 'github_push'. Default 'cli' so existing
                    rows from Phase 5 keep their semantics — the SDK
                    CLI hasn't changed and still uploads via
                    POST /workspaces/me/deployments. NOT NULL because
                    we want every new row to declare which path it
                    came from.

  source_commit_sha Optional. Set when source='github_push' to record
                    which GitHub commit produced this deploy. NULL on
                    every CLI deploy (we don't know the user's local
                    git state, and even if we did the SDK CLI doesn't
                    forward it).

Both columns are populated for new rows by the application layer
(see main.py: upload_deployment for source='cli', _queue_github_redeploy
for source='github_push').

The dashboard's Deployments panel reads these to render an inline
"pushed by GitHub at <short SHA>" vs "uploaded via CLI" hint. Phase 5's
worker doesn't read them — it only cares about status + desired_state.

Revision ID: 0017
Revises: 0016
Create Date: 2026-04-30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # `source` defaults to 'cli' on backfill so existing rows take the
    # right semantics without an explicit data migration. We keep the
    # server_default so future inserts that omit the column (none in
    # current code, but defensive) also land as 'cli'.
    op.add_column(
        "deployments",
        sa.Column(
            "source",
            sa.String(length=32),
            nullable=False,
            server_default="cli",
        ),
    )
    op.add_column(
        "deployments",
        sa.Column(
            "source_commit_sha",
            sa.String(length=64),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("deployments", "source_commit_sha")
    op.drop_column("deployments", "source")
