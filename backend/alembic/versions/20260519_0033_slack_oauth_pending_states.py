"""Phase 19.2: slack_oauth_pending_states — short-lived state store
for the Slack OAuth start → callback hop.

Same shape as oauth_pending_states (Phase 17.3, Google OAuth) but
without the PKCE code_verifier — Slack's OAuth v2 flow doesn't use
PKCE. A separate table keeps the two flows from sharing state-format
assumptions (and lets the cleanup cron reap each on its own TTL).

`installed_by_user_id` tracks which Lightsei user kicked off the
install; we stamp the same value onto `slack_workspaces.installed_by_user_id`
on callback so the audit trail survives.

Revision ID: 0033
Revises: 0032
Create Date: 2026-05-19
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: Union[str, None] = "0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "slack_oauth_pending_states",
        sa.Column("state", sa.String(length=128), primary_key=True),
        sa.Column(
            "lightsei_workspace_id",
            sa.String,
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "installed_by_user_id",
            sa.String,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("redirect_after", sa.String(length=512), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_slack_oauth_pending_states_expires_at",
        "slack_oauth_pending_states",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_slack_oauth_pending_states_expires_at",
        table_name="slack_oauth_pending_states",
    )
    op.drop_table("slack_oauth_pending_states")
