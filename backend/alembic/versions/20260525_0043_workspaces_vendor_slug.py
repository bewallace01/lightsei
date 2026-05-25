"""Phase 26.1: workspaces.vendor_slug column.

Adds a single nullable, unique column to `workspaces` so an operator
can claim a URL-safe handle for their workspace. The handle drives
the Phase 26.2 consumer-chat URL: `/c/{vendor_slug}`.

No backfill: every existing workspace stays NULL until the operator
explicitly claims one via Phase 26.1's POST endpoint. NULL =
"no slug claimed yet"; the workspace's existing `widget_public_id`
(Phase 21) is still the iframe-embed handle.

Why unique:
  - The slug appears in user-facing URLs, so a collision would
    route consumer traffic to the wrong vendor.
  - Uniqueness lets the consume-side lookup be a plain index scan
    instead of disambiguating across rows.

Why varchar(32):
  - 3-32 chars matches the app-side validator
    (`is_valid_vendor_slug`); the column cap is the upper bound.
  - Short enough to stay legible in a URL, long enough to fit
    reasonable brand names like "acme-customer-success".

Revision ID: 0043
Revises: 0042
Create Date: 2026-05-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0043"
down_revision: Union[str, None] = "0042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("vendor_slug", sa.String(length=32), nullable=True),
    )
    # Plain unique index (not a partial unique) because NULL values
    # do not conflict with each other under SQL semantics, so the
    # full-column unique is safe AND keeps the constraint readable.
    op.create_index(
        "ix_workspaces_vendor_slug",
        "workspaces",
        ["vendor_slug"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workspaces_vendor_slug", table_name="workspaces",
    )
    op.drop_column("workspaces", "vendor_slug")
