"""Phase 20.1: connector_installations — per-workspace per-connector
OAuth token holder for the Phase 20 integration-breadth surface.

One row per active OAuth install. Encrypted token blob holds the
access_token + refresh_token + expires_at as serialized JSON, via the
same secrets_crypto path used by WorkspaceSecret.encrypted_value and
SlackWorkspace.bot_token_encrypted.

The partial-unique index `(workspace_id, connector_type) WHERE
revoked_at IS NULL` mirrors Phase 19.1's slack_workspaces pattern: at
most one active install per (workspace, connector_type) at a time;
revoked rows stay for audit; a fresh install of the same connector
after revoke works without manual cleanup.

Revision ID: 0034
Revises: 0033
Create Date: 2026-05-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0034"
down_revision: Union[str, None] = "0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "connector_installations",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String,
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # 'gmail' | 'google_calendar' | 'google_drive' | ... Validated
        # app-side against CONNECTOR_REGISTRY in
        # backend/connectors/__init__.py.
        sa.Column("connector_type", sa.String(length=64), nullable=False),
        # JSON-serialized {access_token, refresh_token, expires_at,
        # token_type, scope} encrypted via secrets_crypto. Refresh
        # path in 20.2 decrypts, refreshes, re-encrypts in-place.
        sa.Column("encrypted_tokens", sa.LargeBinary, nullable=False),
        # The granted scopes (subset of default_scopes; OAuth providers
        # let users decline). JSONB matches the other ORM JSON columns
        # in this schema (agents.capabilities, runs.payload, etc.).
        sa.Column(
            "scopes",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "installed_by_user_id",
            sa.String,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # The external-provider account this connector binds to (e.g.
        # the Google email). Surfaced in the dashboard so the operator
        # knows which account they connected.
        sa.Column(
            "external_account_email",
            sa.String(length=256),
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
    # One active install per (workspace, connector_type) at a time.
    # Revoked rows are excluded so the partial-unique doesn't fight a
    # revoke-then-reinstall cycle.
    op.create_index(
        "ix_connector_installations_ws_type_active",
        "connector_installations",
        ["workspace_id", "connector_type"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    # Browse query in the dashboard: "list all my connector installs."
    # Non-unique companion index for the workspace-scoped scan.
    op.create_index(
        "ix_connector_installations_workspace_installed_at",
        "connector_installations",
        ["workspace_id", "installed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_connector_installations_workspace_installed_at",
        table_name="connector_installations",
    )
    op.drop_index(
        "ix_connector_installations_ws_type_active",
        table_name="connector_installations",
    )
    op.drop_table("connector_installations")
