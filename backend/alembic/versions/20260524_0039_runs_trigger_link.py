"""Phase 22.4: link runs to the trigger that fired them.

Two new columns on `runs`:

- `triggered_by_trigger_id` (FK → triggers.id, SET NULL on trigger
  delete). The scheduled_run handler pre-creates the run row with
  this set so the /runs page can filter + badge.
- `trigger_kind` (text snapshot of triggers.kind at fire time). The
  FK goes NULL when the trigger is deleted, but we still want the
  /runs badge to render "Triggered by: cron" or "Triggered by:
  webhook" against the historical row — the snapshot makes that
  possible without a JOIN that returns nothing.

Index `(workspace_id, triggered_by_trigger_id)` for the 22.8
/runs?trigger_id= filter so the per-trigger history page renders
in one scan.

Manual runs (no trigger) leave both columns NULL. Same column shape
for the future event-based triggers in 22B; new `trigger_kind`
values become opaque labels on the badge without needing another
migration.

Revision ID: 0039
Revises: 0038
Create Date: 2026-05-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0039"
down_revision: Union[str, None] = "0038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "triggered_by_trigger_id",
            sa.String(length=36),
            sa.ForeignKey("triggers.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "runs",
        sa.Column(
            "trigger_kind",
            sa.String(length=16),
            nullable=True,
        ),
    )
    # /runs?trigger_id= filter (22.8). Partial WHERE keeps the index
    # tight: most rows are manual (NULL trigger_id) and don't need
    # to live in the index.
    op.create_index(
        "ix_runs_workspace_trigger",
        "runs",
        ["workspace_id", "triggered_by_trigger_id"],
        postgresql_where=sa.text("triggered_by_trigger_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_runs_workspace_trigger", table_name="runs")
    op.drop_column("runs", "trigger_kind")
    op.drop_column("runs", "triggered_by_trigger_id")
