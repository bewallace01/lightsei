"""Bind Sign in with Apple accounts to Apple's stable subject.

Revision ID: 0047
Revises: 0046
Create Date: 2026-05-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0047"
down_revision: Union[str, None] = "0046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "end_users",
        sa.Column("apple_sub", sa.String(length=255), nullable=True),
    )
    op.create_unique_constraint(
        "uq_end_users_apple_sub",
        "end_users",
        ["apple_sub"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_end_users_apple_sub",
        "end_users",
        type_="unique",
    )
    op.drop_column("end_users", "apple_sub")
