"""Phase 23.3: workspaces.updated_at column.

PATCH /me/workspaces/{id} (added in 23.3) bumps this on rename so
the dashboard can sort + show "last edited" timestamps. Nullable
with `server_default now()` so the migration backfills existing
rows non-disruptively + new inserts get a value without code
changes.

Standalone tiny migration to keep the 23.3 endpoint diff focused.

Revision ID: 0041
Revises: 0040
Create Date: 2026-05-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0041"
down_revision: Union[str, None] = "0040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "updated_at")
