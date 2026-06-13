"""Phase 35.2: agents.display_name (per-workspace assistant rename).

Nullable: NULL = use the constellation default (assistant_identity). A
value is the owner's chosen name. Display-only; the primary key `name`
(the internal id) is untouched.

Revision ID: 0054
Revises: 0053
Create Date: 2026-06-13
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0054"
down_revision: Union[str, None] = "0053"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("display_name", sa.String(length=80), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agents", "display_name")
