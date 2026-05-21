"""Phase 20.2: connector_oauth_pending_states — short-lived state
store for the Phase 20 connector-install OAuth hop.

Mirrors slack_oauth_pending_states (Phase 19.2) but extends the row
with a `connector_type` column so the callback knows which connector
to bind the resulting install to, and a `code_verifier` column so
the PKCE handshake from Phase 17.3's google_oauth.py works here too.

Why a third pending-states table (after `oauth_pending_states` for
sign-in + `slack_oauth_pending_states` for Slack): each flow has a
non-overlapping schema (sign-in needs no workspace; Slack carries the
team_id; connector OAuth carries connector_type + workspace). Keeping
each in its own table avoids nullable columns + lets each flow's
cleanup cron prune on its own TTL.

Revision ID: 0035
Revises: 0034
Create Date: 2026-05-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: Union[str, None] = "0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "connector_oauth_pending_states",
        sa.Column("state", sa.String(length=128), primary_key=True),
        sa.Column(
            "workspace_id",
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
        # Which connector the install binds to once the callback fires.
        # Validated app-side against CONNECTOR_REGISTRY.
        sa.Column("connector_type", sa.String(length=64), nullable=False),
        # PKCE verifier — sent with the token exchange on callback so
        # Google can verify it matches the challenge the start
        # endpoint sent up.
        sa.Column("code_verifier", sa.String(length=128), nullable=False),
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
        "ix_connector_oauth_pending_states_expires_at",
        "connector_oauth_pending_states",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_connector_oauth_pending_states_expires_at",
        table_name="connector_oauth_pending_states",
    )
    op.drop_table("connector_oauth_pending_states")
