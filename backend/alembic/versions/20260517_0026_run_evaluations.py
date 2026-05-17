"""run_evaluations: judge-LLM verdicts on completed runs (Phase 14.2)

Backs Phase 14's continuous-evaluation guardrail (MEMORY.md "Five
guardrail layers" #5). The eval runner samples completed runs via
`backend/eval_sampler.py`, asks a judge LLM (claude-sonnet-4-6) for a
verdict, and writes one row here per evaluated run. The dashboard
surfaces verdict rollups on /agents; 12D.3's auto-tuner reads the
same signal via the SDK helper.

Indexes:
  - (workspace_id, agent_name, created_at DESC): the /agents quality
    column + /agents/{name} verdict breakdown both lead with this.
  - (workspace_id, verdict, created_at DESC): "recent bads" queries
    (the dashboard's red-flag list + 12D.2's narration of regressions).

FK on run_id cascades on delete so purging a workspace doesn't leave
orphan eval rows. judge_cost_usd lands on `lightsei.system` via the
Run row the eval runner writes (same pattern as generation calls),
so the workspace monthly budget gate covers judge spend too.

Revision ID: 0026
Revises: 0025
Create Date: 2026-05-17
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "run_evaluations",
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
        sa.Column("judge_model", sa.String(length=64), nullable=False),
        # Verdict is one of: 'good' | 'borderline' | 'bad'. Stored as a
        # short string rather than an enum so adding a future verdict
        # (e.g. 'unparseable') is a code-only change.
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("reasons", JSONB(), nullable=False),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=False),
        sa.Column("judge_tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("judge_tokens_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "judge_cost_usd",
            sa.Numeric(12, 6),
            nullable=False,
            server_default="0",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "idx_run_evaluations_ws_agent_created",
        "run_evaluations",
        ["workspace_id", "agent_name", sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_run_evaluations_ws_verdict_created",
        "run_evaluations",
        ["workspace_id", "verdict", sa.text("created_at DESC")],
    )
    # Unique-ish guard so the sampler's "skip already-evaluated runs"
    # check has DB enforcement behind it, not just the application-level
    # NOT EXISTS clause. One verdict per (run_id, judge_model) pair —
    # re-evaluating with a different judge later is allowed.
    op.create_index(
        "idx_run_evaluations_run_judge",
        "run_evaluations",
        ["run_id", "judge_model"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_run_evaluations_run_judge", table_name="run_evaluations"
    )
    op.drop_index(
        "idx_run_evaluations_ws_verdict_created", table_name="run_evaluations"
    )
    op.drop_index(
        "idx_run_evaluations_ws_agent_created", table_name="run_evaluations"
    )
    op.drop_table("run_evaluations")
