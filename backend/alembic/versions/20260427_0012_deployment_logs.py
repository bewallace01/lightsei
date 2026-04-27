"""deployment_logs (Phase 5.2)

Revision ID: 0012
Revises: 0011
Create Date: 2026-04-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "deployment_logs",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True,
        ),
        sa.Column(
            "deployment_id",
            sa.String(),
            sa.ForeignKey("deployments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stream", sa.String(), nullable=False),  # stdout|stderr|system
        sa.Column("line", sa.Text(), nullable=False),
    )
    op.create_index(
        "idx_deployment_logs_dep",
        "deployment_logs",
        ["deployment_id", "id"],
    )


def downgrade() -> None:
    op.drop_index("idx_deployment_logs_dep", table_name="deployment_logs")
    op.drop_table("deployment_logs")
