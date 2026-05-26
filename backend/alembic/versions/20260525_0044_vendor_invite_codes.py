"""Phase 27.1: vendor invite codes + per-vendor end-user settings.

Two pieces:

1. `vendor_invite_codes` table. An operator mints N codes for their
   workspace; end users redeem one via Phase 27.2's
   `/me/end-user/redeem-invite` endpoint to create an
   `end_user_vendor_links` row. Single-use (consumed_at sticks),
   30-day TTL by default (enforced at issue time, not as a DB
   constraint — the operator can mint a longer-lived code later).
   `consumed_by_end_user_id` is the audit trail of who redeemed
   each code; SET NULL on end-user delete so the codes row stays
   in place for vendor-side bookkeeping.

2. New columns on `end_user_vendor_links`:
   - `display_name_override` (nullable). Lets the end user show as
     "Alice Smith" to vendor A and "alice@example.com" to vendor B
     without forking the EndUser row. Phase 27.5 wires the UI.
   - `notification_pref` (`all` / `mentions` / `off`, default
     `all`). Phase 28 push delivery reads this gate before sending.

Note: `removed_at` already landed in alembic 0042 per the Phase 25.1
spec block (soft-revoke pointer); we don't add it again here.

Revision ID: 0044
Revises: 0043
Create Date: 2026-05-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0044"
down_revision: Union[str, None] = "0043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---------- vendor_invite_codes ---------- #

    op.create_table(
        "vendor_invite_codes",
        # UUID-shaped code is also the primary key; the URL the
        # end user types/pastes IS this value. No separate hash
        # because the code is single-use + short-lived; a leak
        # reveals at most one redemption.
        sa.Column("code", sa.String(length=64), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String,
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "consumed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        # SET NULL (not CASCADE) so deleting the redeeming end user
        # doesn't blow away the audit-trail row on the vendor side.
        # The link row is what's CASCADE'd on end_user delete; this
        # row just remembers a code was used.
        sa.Column(
            "consumed_by_end_user_id",
            sa.String,
            sa.ForeignKey("end_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # Vendor-side list: "show me my outstanding + recently-used
    # invite codes." Filtering on workspace_id, ordering by
    # created_at DESC.
    op.create_index(
        "ix_vendor_invite_codes_workspace_created",
        "vendor_invite_codes",
        ["workspace_id", sa.text("created_at DESC")],
    )

    # ---------- end_user_vendor_links new columns ---------- #

    op.add_column(
        "end_user_vendor_links",
        sa.Column("display_name_override", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "end_user_vendor_links",
        sa.Column(
            "notification_pref",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'all'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("end_user_vendor_links", "notification_pref")
    op.drop_column("end_user_vendor_links", "display_name_override")
    op.drop_index(
        "ix_vendor_invite_codes_workspace_created",
        table_name="vendor_invite_codes",
    )
    op.drop_table("vendor_invite_codes")
