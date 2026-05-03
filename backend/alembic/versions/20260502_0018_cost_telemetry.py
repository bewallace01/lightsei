"""cost telemetry: model_pricing + workspace_pricing_overrides
+ runs.cost_usd + workspaces.budget_usd_monthly (Phase 11B.1)

Lays the schema groundwork for the home-page cost panel and the
workspace-level monthly spend cap.

Four schema changes plus one data backfill:

  1. model_pricing
       (provider, model) PK, prices in $/1M tokens, effective_from
       window for future historical-pricing work. Mirrors
       `pricing.PRICING`; re-asserted on startup via
       `pricing.seed_model_pricing(session)` so a release can update
       prices without a manual UPDATE. The migration also seeds the
       table once so this migration is fully self-contained for fresh
       installs (existing prod environments will overwrite it on the
       very next startup, which is fine).

  2. workspace_pricing_overrides
       Per-workspace rate overrides for negotiated enterprise pricing.
       Empty in v1 — the cost computation path doesn't read this yet.
       Reserved so future Phase 12+ work has a place to put rates
       without another schema migration.

  3. runs.cost_usd  numeric(12, 6) NOT NULL DEFAULT 0
       Cached per-run cost. Incrementally summed in main.py's event
       ingest path on each `llm_call_completed` event so dashboard
       rollups don't re-join events × pricing on every render. The
       backfill UPDATE below populates the column for runs that
       already exist.

  4. workspaces.budget_usd_monthly  numeric(10, 2) NULL
       Optional workspace-level monthly spend cap. NULL = no cap.
       When set and reached, runs in this workspace get denied with
       the same UX path as Phase 2's per-agent daily cap (handler
       lands in a follow-on commit).

Backfill: walks every existing `runs` row and recomputes its cost
from the events table by joining against the freshly-seeded
`model_pricing` rows. Runs whose llm_call_completed events reference
unpriced models contribute 0, matching `compute_cost_usd`'s default.

Revision ID: 0018
Revises: 0017
Create Date: 2026-05-02
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Inline copy of `pricing.PRICING` so the migration doesn't import the
# app module — keeps alembic runs self-contained even when the broader
# Python environment is in a weird state. Kept identical to the SDK's
# source-of-truth dict; will get re-asserted on startup so any drift
# heals automatically on the next deploy.
_INITIAL_PRICING: list[tuple[str, str, float, float]] = [
    # (provider, model, input_per_million_usd, output_per_million_usd)
    ("openai", "gpt-4o", 2.50, 10.00),
    ("openai", "gpt-4o-2024-08-06", 2.50, 10.00),
    ("openai", "gpt-4o-2024-11-20", 2.50, 10.00),
    ("openai", "gpt-4o-mini", 0.15, 0.60),
    ("openai", "gpt-4o-mini-2024-07-18", 0.15, 0.60),
    ("openai", "gpt-4-turbo", 10.00, 30.00),
    ("openai", "gpt-4", 30.00, 60.00),
    ("openai", "gpt-3.5-turbo", 0.50, 1.50),
    ("openai", "o1", 15.00, 60.00),
    ("openai", "o1-mini", 3.00, 12.00),
    ("openai", "o3-mini", 1.10, 4.40),
    ("anthropic", "claude-opus-4-7", 15.00, 75.00),
    ("anthropic", "claude-sonnet-4-6", 3.00, 15.00),
    ("anthropic", "claude-haiku-4-5", 0.80, 4.00),
    ("anthropic", "claude-haiku-4-5-20251001", 0.80, 4.00),
]


def upgrade() -> None:
    op.create_table(
        "model_pricing",
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column(
            "input_per_million_usd", sa.Numeric(10, 4), nullable=False
        ),
        sa.Column(
            "output_per_million_usd", sa.Numeric(10, 4), nullable=False
        ),
        sa.Column(
            "effective_from",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "deprecated_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("provider", "model"),
    )

    op.create_table(
        "workspace_pricing_overrides",
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column(
            "input_per_million_usd", sa.Numeric(10, 4), nullable=False
        ),
        sa.Column(
            "output_per_million_usd", sa.Numeric(10, 4), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("workspace_id", "model"),
    )

    op.add_column(
        "runs",
        sa.Column(
            "cost_usd",
            sa.Numeric(12, 6),
            nullable=False,
            server_default="0",
        ),
    )

    op.add_column(
        "workspaces",
        sa.Column(
            "budget_usd_monthly", sa.Numeric(10, 2), nullable=True
        ),
    )

    # Seed model_pricing once. Subsequent boots re-assert from
    # pricing.seed_model_pricing(); this initial load just makes the
    # migration self-contained for fresh installs and tests.
    bind = op.get_bind()
    for provider, model, in_per_m, out_per_m in _INITIAL_PRICING:
        bind.execute(
            sa.text(
                """
                INSERT INTO model_pricing
                  (provider, model, input_per_million_usd,
                   output_per_million_usd)
                VALUES (:provider, :model, :in_per_m, :out_per_m)
                ON CONFLICT (provider, model) DO NOTHING
                """
            ),
            {
                "provider": provider,
                "model": model,
                "in_per_m": in_per_m,
                "out_per_m": out_per_m,
            },
        )

    # Backfill runs.cost_usd from existing llm_call_completed events ×
    # the freshly-seeded pricing table. Unknown models contribute 0
    # via the LEFT JOIN — matches compute_cost_usd's default.
    op.execute(
        """
        UPDATE runs r
        SET cost_usd = COALESCE(sub.cost, 0)
        FROM (
            SELECT
                e.run_id,
                SUM(
                    COALESCE((e.payload->>'input_tokens')::numeric, 0)
                      * COALESCE(mp.input_per_million_usd, 0) / 1000000.0
                    +
                    COALESCE((e.payload->>'output_tokens')::numeric, 0)
                      * COALESCE(mp.output_per_million_usd, 0) / 1000000.0
                ) AS cost
            FROM events e
            LEFT JOIN model_pricing mp
              ON mp.model = e.payload->>'model'
            WHERE e.kind = 'llm_call_completed'
            GROUP BY e.run_id
        ) sub
        WHERE r.id = sub.run_id
        """
    )


def downgrade() -> None:
    op.drop_column("workspaces", "budget_usd_monthly")
    op.drop_column("runs", "cost_usd")
    op.drop_table("workspace_pricing_overrides")
    op.drop_table("model_pricing")
