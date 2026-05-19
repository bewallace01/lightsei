"""Phase 19.1: Slack chat-surface schema.

Three tables form the backbone every other Phase 19.x sub-task reads from:

- `slack_workspaces`: one row per Slack workspace that has installed the
  Lightsei Slack app. Owns the encrypted bot OAuth token + the
  one-to-one binding from `slack_team_id` to `lightsei_workspace_id`.

- `slack_channels`: one row per Slack channel the Lightsei app has been
  mentioned in. Each carries the operator-set `sensitivity_level` that
  the Phase 19.4 chat orchestrator uses to filter which bots can be
  reached from that channel (mirrors the Phase 16 trust-zone story).
  Channels start `opted_in=False` — silent until the operator turns
  them on from the dashboard.

- `slack_events`: idempotency log keyed on Slack's `event.event_id`.
  Slack retries deliveries up to 3 times within an hour and again at
  longer intervals; without this we'd double-route the same mention.

Revision ID: 0032
Revises: 0031
Create Date: 2026-05-19
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: Union[str, None] = "0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # slack_workspaces — one row per Slack install. Bot token encrypted
    # via secrets_crypto (same pattern as WorkspaceSecret.encrypted_value).
    # `lightsei_workspace_id` is the binding: a Slack workspace can be
    # connected to at most one Lightsei workspace at a time (uniqueness
    # below). Revoke = set `revoked_at`; we keep the row for audit.
    op.create_table(
        "slack_workspaces",
        sa.Column("slack_team_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "lightsei_workspace_id",
            sa.String,
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("team_name", sa.String(length=256), nullable=False),
        # bot_token = xoxb-... Encrypted, never logged.
        sa.Column("bot_token_encrypted", sa.LargeBinary, nullable=False),
        sa.Column("bot_user_id", sa.String(length=64), nullable=False),
        sa.Column(
            "installed_by_user_id",
            sa.String,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "installed_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    # One Lightsei workspace per Slack workspace at a time. Filtered
    # to non-revoked installs so a revoked+re-installed cycle works
    # without a manual cleanup. Postgres partial-unique index.
    op.create_index(
        "ix_slack_workspaces_lightsei_workspace_active",
        "slack_workspaces",
        ["lightsei_workspace_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    # slack_channels — composite PK (slack_team_id, channel_id). One
    # Lightsei workspace can have many Slack workspaces eventually
    # (multi-team Slack installs); each channel belongs to exactly one
    # Slack workspace. lightsei_workspace_id is denormalized so the
    # chat orchestrator can read channel + workspace in one query
    # without joining slack_workspaces.
    op.create_table(
        "slack_channels",
        sa.Column("slack_team_id", sa.String(length=64), nullable=False),
        sa.Column("channel_id", sa.String(length=64), nullable=False),
        sa.Column(
            "lightsei_workspace_id",
            sa.String,
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_name", sa.String(length=256), nullable=False),
        # Phase 16 sensitivity ladder. Same column type + default as
        # agents.sensitivity_level. Validated app-side via
        # _VALID_SENSITIVITY_LEVELS in models.py — Postgres-level check
        # not added because the existing agents column doesn't have one
        # either, and 16.1 documented the choice.
        sa.Column(
            "sensitivity_level",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'internal'"),
        ),
        # Channels are silent until the operator opts in from the
        # dashboard. Without this default, the Lightsei Slack app would
        # respond in every channel it gets added to, which is exactly
        # the wedge story's opposite.
        sa.Column(
            "opted_in",
            sa.Boolean,
            nullable=False,
            server_default=sa.false(),
        ),
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
        sa.PrimaryKeyConstraint(
            "slack_team_id", "channel_id",
            name="pk_slack_channels",
        ),
        sa.ForeignKeyConstraint(
            ["slack_team_id"],
            ["slack_workspaces.slack_team_id"],
            ondelete="CASCADE",
        ),
    )
    # The chat orchestrator's primary query is "given Lightsei workspace
    # X + sensitivity Y, which channels are opted in?" — index leftmost
    # on workspace, then sensitivity for the filter.
    op.create_index(
        "ix_slack_channels_workspace_sensitivity",
        "slack_channels",
        ["lightsei_workspace_id", "sensitivity_level"],
    )

    # slack_events — idempotency log. Slack retries delivery; without
    # this we'd dispatch the same mention twice. event_id is unique
    # within a Slack team but we key on (slack_team_id, event_id) to be
    # safe across tenants.
    op.create_table(
        "slack_events",
        sa.Column("slack_team_id", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "slack_team_id", "event_id",
            name="pk_slack_events",
        ),
    )
    # Used to age out old idempotency entries (cron job, not in this
    # sub-task). Index makes the cleanup query cheap.
    op.create_index(
        "ix_slack_events_received_at",
        "slack_events",
        ["received_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_slack_events_received_at",
        table_name="slack_events",
    )
    op.drop_table("slack_events")

    op.drop_index(
        "ix_slack_channels_workspace_sensitivity",
        table_name="slack_channels",
    )
    op.drop_table("slack_channels")

    op.drop_index(
        "ix_slack_workspaces_lightsei_workspace_active",
        table_name="slack_workspaces",
    )
    op.drop_table("slack_workspaces")
