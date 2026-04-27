"""deployments + deployment_blobs (Phase 5.1)

Revision ID: 0011
Revises: 0010
Create Date: 2026-04-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Source-of-truth bytes for an uploaded deployment. Kept in a separate
    # table so deployment metadata survives blob cleanup and so a future
    # move to object storage (Cloudflare R2) is a one-table swap, not a
    # column-level rewrite.
    op.create_table(
        "deployment_blobs",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column(
            "workspace_id",
            sa.String(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "idx_deployment_blobs_workspace",
        "deployment_blobs",
        ["workspace_id"],
    )

    op.create_table(
        "deployments",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column(
            "workspace_id",
            sa.String(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_name", sa.String(), nullable=False),
        # Lifecycle observed by the system.
        # queued → building → running → stopped|failed
        sa.Column(
            "status", sa.String(),
            nullable=False, server_default="queued",
        ),
        # What the user *wants*. Decoupled from status so the worker can
        # see "user wants stop, current state is running" and act.
        sa.Column(
            "desired_state", sa.String(),
            nullable=False, server_default="running",
        ),
        sa.Column(
            "source_blob_id",
            sa.String(),
            sa.ForeignKey("deployment_blobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("claimed_by", sa.String(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "idx_deployments_ws_agent",
        "deployments",
        ["workspace_id", "agent_name", sa.text("created_at DESC")],
    )
    # Worker poll path: cheap predicate filter for the claim query.
    op.create_index(
        "idx_deployments_claimable",
        "deployments",
        ["status", "desired_state"],
    )


def downgrade() -> None:
    op.drop_index("idx_deployments_claimable", table_name="deployments")
    op.drop_index("idx_deployments_ws_agent", table_name="deployments")
    op.drop_table("deployments")
    op.drop_index("idx_deployment_blobs_workspace", table_name="deployment_blobs")
    op.drop_table("deployment_blobs")
