"""generation_jobs: async job queue for long-running LLM calls (Phase 12C.6)

One table backs both `/agents/generate` and `/teams/plan`. The endpoints
enqueue a row and return 202 + job_id; the in-process runner in
`backend/jobs.py` claims pending rows, dispatches by `kind`, and writes
`result_payload` or `error` on terminal state. The dashboard polls
`GET /workspaces/me/generation-jobs/{id}` to surface progress.

Why one table for both kinds: the lifecycle (pending → running → success
| failed), the authz model (workspace-scoped), and the payload shape
(arbitrary JSONB request + JSONB result) are identical. The `kind`
column is the dispatch discriminator; splitting into two tables would
duplicate every index + every poll endpoint.

Indexes:
  - (status, created_at): runner's pending-picker. Status leftmost so
    the same index answers "is anything pending?" probes cheaply.
  - (workspace_id, created_at DESC): poll endpoint + future list view.

No auto-retry in v1. On `failed`, the dashboard surfaces the error and
the user retries from the UI (which enqueues a fresh row).

Revision ID: 0025
Revises: 0024
Create Date: 2026-05-16
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "generation_jobs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("request_payload", JSONB(), nullable=False),
        sa.Column("result_payload", JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_generation_jobs_status_created",
        "generation_jobs",
        ["status", "created_at"],
    )
    op.create_index(
        "idx_generation_jobs_workspace",
        "generation_jobs",
        ["workspace_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("idx_generation_jobs_workspace", table_name="generation_jobs")
    op.drop_index(
        "idx_generation_jobs_status_created", table_name="generation_jobs"
    )
    op.drop_table("generation_jobs")
