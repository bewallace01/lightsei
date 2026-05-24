"""Phase 22.1: triggers schema.

One new table drives the scheduled-bots + webhook-triggers surface:

- `triggers`: one row per configured trigger on an agent. Kind is
  `cron` (recurring schedule via croniter) or `webhook` (token in URL,
  fired by external POST). One agent can have many triggers; same
  kind can repeat. Operator-driven from the per-agent triggers panel
  (22.7).

The hot scheduler query is `WHERE enabled AND kind='cron' AND
next_run_at <= NOW()` so the index covers (enabled, next_run_at).
The per-agent list panel scans (workspace_id, agent_name). The
webhook endpoint looks up triggers by sha256-hashed token.

Agent reference is `agent_name: String` not a real FK because the
agents table has a composite PK (workspace_id, name); same pattern
as deployments + widget_conversations. App-side queries resolve via
the (workspace_id, agent_name) pair.

`last_run_id` IS a real FK because runs.id is single-column. On run
delete, set to NULL so the trigger row survives.

Revision ID: 0038
Revises: 0037
Create Date: 2026-05-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0038"
down_revision: Union[str, None] = "0037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "triggers",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String,
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Agent reference: composite-PK table, so we store the name
        # not a FK. (workspace_id, agent_name) is the natural lookup.
        sa.Column("agent_name", sa.String(length=128), nullable=False),
        # 'cron' or 'webhook'. App-side validation against
        # _VALID_TRIGGER_KINDS; no DB enum so adding a kind later
        # (event-based in 22B) doesn't need a migration.
        sa.Column("kind", sa.String(length=16), nullable=False),
        # 5-field cron expression. Required when kind='cron', NULL
        # when kind='webhook'. App-side enforces the conditional.
        sa.Column("schedule", sa.String(length=128), nullable=True),
        # sha256 of the plaintext token (32 bytes URL-safe random).
        # Required when kind='webhook'; NULL when kind='cron'.
        # Plaintext only exists at create-time, returned to the
        # operator once. Hash is unique across triggers for the
        # token-lookup path on the public webhook endpoint.
        sa.Column("webhook_token_hash", sa.String(length=64), nullable=True),
        # Short operator-typed label, e.g. "morning digest". Shows
        # in the dashboard list + on /runs trigger badge tooltip.
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
        ),
        # Pre-computed next-fire time for cron triggers. NULL for
        # webhook kind (they fire on demand). Scheduler's hot
        # query: WHERE enabled AND kind='cron' AND next_run_at <= NOW().
        sa.Column(
            "next_run_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "last_run_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        # FK to the most recent run this trigger spawned. Real FK
        # (runs.id is single-column). SET NULL on run delete so
        # the trigger survives run cleanup.
        sa.Column(
            "last_run_id",
            sa.String,
            sa.ForeignKey("runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Snapshot of last_run.status, kept on the trigger row so
        # the dashboard list query renders without a JOIN.
        sa.Column("last_run_status", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
    )

    # Scheduler hot query: enabled cron triggers due to fire,
    # ordered by next_run_at. Partial-where keeps the index small
    # (skips webhook rows, which always have next_run_at NULL).
    op.create_index(
        "ix_triggers_due",
        "triggers",
        ["enabled", "next_run_at"],
        postgresql_where=sa.text("kind = 'cron'"),
    )
    # Per-agent triggers list (dashboard panel).
    op.create_index(
        "ix_triggers_workspace_agent",
        "triggers",
        ["workspace_id", "agent_name"],
    )
    # Webhook lookup: sha256 the URL token, find the trigger.
    # Partial-unique so multiple cron triggers (all NULL hashes)
    # coexist; only populated hashes must be unique.
    op.create_index(
        "ix_triggers_webhook_token",
        "triggers",
        ["webhook_token_hash"],
        unique=True,
        postgresql_where=sa.text("webhook_token_hash IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_triggers_webhook_token", table_name="triggers")
    op.drop_index("ix_triggers_workspace_agent", table_name="triggers")
    op.drop_index("ix_triggers_due", table_name="triggers")
    op.drop_table("triggers")
