"""Phase 10B: github_connections + github_repos (multi-repo).

Splits the single per-workspace github_integrations row into a
workspace-level connection (the OAuth/PAT token) plus N repo rows, so one
workspace can watch many repos with a single auth.

Additive + non-destructive: github_integrations is left in place (the
webhook still reads it until the 10B cutover). Every existing
github_integrations row is backfilled into a connection (auth_kind='pat')
+ one repo, so live push-to-deploy data carries over.

Revision ID: 0049
Revises: 0048
Create Date: 2026-06-07
"""
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0049"
down_revision: Union[str, None] = "0048"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "github_connections",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("encrypted_token", sa.Text(), nullable=False),
        sa.Column("auth_kind", sa.String(length=16), nullable=False, server_default="pat"),
        sa.Column("github_login", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "github_repos",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "connection_id",
            sa.String(),
            sa.ForeignKey("github_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("repo_owner", sa.String(length=255), nullable=False),
        sa.Column("repo_name", sa.String(length=255), nullable=False),
        sa.Column("branch", sa.String(length=255), nullable=False, server_default="main"),
        sa.Column("encrypted_webhook_secret", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "uq_github_repos_ws_owner_name",
        "github_repos",
        ["workspace_id", "repo_owner", "repo_name"],
        unique=True,
    )
    op.create_index(
        "ix_github_repos_owner_name", "github_repos", ["repo_owner", "repo_name"]
    )

    # Backfill from github_integrations so existing connections + their
    # push-to-deploy keep working under the new model.
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT workspace_id, repo_owner, repo_name, branch, "
            "encrypted_pat, encrypted_webhook_secret, is_active, "
            "created_at, updated_at FROM github_integrations"
        )
    ).fetchall()
    for r in rows:
        conn_id = str(uuid.uuid4())
        bind.execute(
            sa.text(
                "INSERT INTO github_connections "
                "(id, workspace_id, encrypted_token, auth_kind, github_login, created_at, updated_at) "
                "VALUES (:id, :ws, :tok, 'pat', NULL, :c, :u)"
            ),
            {"id": conn_id, "ws": r.workspace_id, "tok": r.encrypted_pat,
             "c": r.created_at, "u": r.updated_at},
        )
        bind.execute(
            sa.text(
                "INSERT INTO github_repos "
                "(id, workspace_id, connection_id, repo_owner, repo_name, branch, "
                "encrypted_webhook_secret, is_active, created_at, updated_at) "
                "VALUES (:id, :ws, :cid, :o, :n, :b, :wh, :act, :c, :u)"
            ),
            {"id": str(uuid.uuid4()), "ws": r.workspace_id, "cid": conn_id,
             "o": r.repo_owner, "n": r.repo_name, "b": r.branch,
             "wh": r.encrypted_webhook_secret, "act": r.is_active,
             "c": r.created_at, "u": r.updated_at},
        )


def downgrade() -> None:
    op.drop_index("ix_github_repos_owner_name", table_name="github_repos")
    op.drop_index("uq_github_repos_ws_owner_name", table_name="github_repos")
    op.drop_table("github_repos")
    op.drop_table("github_connections")
