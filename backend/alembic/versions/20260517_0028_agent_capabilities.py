"""capabilities column on agents (Phase 16.2)

Adds the per-agent capability allow-list that 16.3's SDK gate refuses
ops against. Separate revision from 0027 because the capability
vocabulary lives in its own module (`backend/capabilities.py`) and
will grow independently of the sensitivity ladder — keeping the two
migrations split makes it easy to roll one back without disturbing
the other.

JSONB rather than `String[]`: capabilities are a list-of-strings
shape now, but the model may grow to hold per-capability metadata
({"name": "internet", "max_requests_per_min": 60}, etc.). JSONB
keeps that future open without another schema change. Default `[]`
so new and existing agents land on "no capabilities granted yet"
rather than getting accidental access through a missing column.

Validation lives in `backend/capabilities.py:validate_capability_list`
and at the PATCH endpoint; the DB constraint is just NOT NULL.

Revision ID: 0028
Revises: 0027
Create Date: 2026-05-17
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # NOT NULL DEFAULT '[]'. Postgres backfills existing rows with the
    # literal default during the ADD COLUMN; the explicit UPDATE
    # afterward is belt-and-suspenders insurance against any future
    # migration that drops the default before relying on the column.
    op.add_column(
        "agents",
        sa.Column(
            "capabilities",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.execute(
        "UPDATE agents SET capabilities = '[]'::jsonb "
        "WHERE capabilities IS NULL"
    )


def downgrade() -> None:
    op.drop_column("agents", "capabilities")
