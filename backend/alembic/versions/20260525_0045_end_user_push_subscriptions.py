"""Phase 28.1: end-user web-push subscriptions.

Stores one row per (end_user, device) pair. The browser's
`PushManager.subscribe()` returns three pieces — `endpoint` (a
push-service URL, vendor-specific to the user's browser), and two
crypto fields (`p256dh` + `auth`) needed for signing/encrypting
payloads via VAPID.

Composite unique on `(end_user_id, endpoint)` so re-subscribing
from the same device (e.g. the user revoked + re-granted browser
permission) updates an existing row instead of accumulating dup
subscriptions. The Phase 28.5 subscribe endpoint uses upsert.

`last_used_at` is bumped each successful send so an audit can
distinguish active devices from stale rows. `revoked_at` is set
when the push service returns 410 Gone (subscription is dead);
the Phase 28.2 send helper sets it + skips the row on future
fan-outs.

CASCADE on end_user_id so deleting an end user blows away all
their push subscriptions; no orphan rows pointing at vanished
identities.

Revision ID: 0045
Revises: 0044
Create Date: 2026-05-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0045"
down_revision: Union[str, None] = "0044"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "end_user_push_subscriptions",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column(
            "end_user_id",
            sa.String,
            sa.ForeignKey("end_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The push-service URL the browser hands back from
        # PushManager.subscribe(). Length varies by vendor; Text
        # is safest. The composite unique below pairs this with
        # end_user_id for the upsert key.
        sa.Column("endpoint", sa.Text, nullable=False),
        # VAPID payload-encryption keys. Both are base64-encoded
        # ECDH/HMAC values; storing as text is the standard shape
        # the py-vapid + pywebpush libraries expect.
        sa.Column("p256dh", sa.Text, nullable=False),
        sa.Column("auth", sa.Text, nullable=False),
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
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        # Re-subscribing from the same device (e.g. permission
        # revoked + re-granted) hits this constraint, which the
        # Phase 28.5 endpoint uses as the upsert key.
        sa.UniqueConstraint(
            "end_user_id", "endpoint",
            name="uq_end_user_push_subscriptions_end_user_endpoint",
        ),
    )
    # Per-end-user lookup: "every active subscription for this
    # end user" is the fan-out query Phase 28.2's send_to_end_user
    # runs on every push event. Partial-where keeps the index small
    # (revoked rows pile up but we never scan them).
    op.create_index(
        "ix_end_user_push_subscriptions_active",
        "end_user_push_subscriptions",
        ["end_user_id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_end_user_push_subscriptions_active",
        table_name="end_user_push_subscriptions",
    )
    op.drop_table("end_user_push_subscriptions")
