"""Phase 29.4: end-user APNS device tokens.

Stores one row per (end_user, device) pair, parallel to the Phase
28.1 end_user_push_subscriptions table but keyed on Apple's APNS
device tokens instead of Web Push endpoints.

`device_token` is the hex-encoded APNS token the iOS app receives
from UIApplication.registerForRemoteNotifications() +
didRegisterForRemoteNotificationsWithDeviceToken. `bundle_id`
distinguishes prod (com.lightsei.app) from any future TestFlight
build with a different bundle (com.lightsei.app.beta), so the
APNS sender targets the right APNS topic.

`environment` is "sandbox" (development APNS gateway) or
"production". The iOS APNS push environment is decided at
provisioning time; the sender picks the gateway URL based on this
column.

Composite unique on `(end_user_id, device_token)` so re-registering
from the same device (token rotates ~monthly or after reinstall)
upserts rather than accumulating dup rows.

CASCADE on end_user_id matches the Phase 28.1 pattern.

Revision ID: 0046
Revises: 0045
Create Date: 2026-05-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0046"
down_revision: Union[str, None] = "0045"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "end_user_apns_tokens",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column(
            "end_user_id",
            sa.String,
            sa.ForeignKey("end_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Hex-encoded device token. Apple's tokens are 32 bytes →
        # 64 hex chars today, but the format has rotated historically;
        # Text is safest.
        sa.Column("device_token", sa.Text, nullable=False),
        # APNS topic / bundle id (e.g. "com.lightsei.app"). Stored
        # per row in case TestFlight + App Store builds coexist.
        sa.Column("bundle_id", sa.String(128), nullable=False),
        # "sandbox" or "production". Picks the APNS gateway URL.
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_used_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        # Set when APNS returns 410 BadDeviceToken or
        # 410 Unregistered. Phase 29.4 sender clears the row from
        # the active fan-out via the partial index below.
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "end_user_id", "device_token",
            name="uq_end_user_apns_tokens_end_user_token",
        ),
    )
    # Active-rows partial index — the fan-out hot path is
    # "every live APNS token for this end user."
    op.create_index(
        "ix_end_user_apns_tokens_active",
        "end_user_apns_tokens",
        ["end_user_id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_end_user_apns_tokens_active",
        table_name="end_user_apns_tokens",
    )
    op.drop_table("end_user_apns_tokens")
