"""Phase 15.2: run_behavioral_violations (guardrail layer 4).

One row per (run_id, rule) behavioral-rule violation detected across a
run's event stream by backend/behavioral_rules.py: a loop, runaway token
spend, or an escalating-permission pattern. Advisory in v1 (recorded +
surfaced, the run is not halted).

Indexes:
  (workspace_id, agent_name, created_at DESC) for the agent/run views.
  unique (run_id, rule) so re-evaluating a run updates rather than
  duplicates a given rule's violation.

Revision ID: 0048
Revises: 0047
Create Date: 2026-06-07
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "0048"
down_revision: Union[str, None] = "0047"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "run_behavioral_violations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            sa.String(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_name", sa.String(), nullable=False),
        # 'loop' | 'runaway_tokens' | 'escalating_permissions'. Short
        # string rather than an enum so a new rule is a code-only change.
        sa.Column("rule", sa.String(length=48), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),  # 'warn' | 'block'
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("details", JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "idx_behavioral_ws_agent_created",
        "run_behavioral_violations",
        ["workspace_id", "agent_name", sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_behavioral_run_rule",
        "run_behavioral_violations",
        ["run_id", "rule"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("idx_behavioral_run_rule", table_name="run_behavioral_violations")
    op.drop_index("idx_behavioral_ws_agent_created", table_name="run_behavioral_violations")
    op.drop_table("run_behavioral_violations")
