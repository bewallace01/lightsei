"""Phase 17.1: schema backbone for self-serve auth + billing.

Three things land together because they're all storage-only and
sub-tasks 17.2-17.7 want all of them present:

1. users: email_verified, auth_provider, google_user_id. Lets a
   user be created via magic-link or Google OAuth (not just the
   existing API-key signup) and matched on return via the
   provider's stable identifier.

2. workspaces: stripe_customer_id, stripe_subscription_id,
   plan_tier, free_credits_remaining_usd. Lets the workspace
   become a Stripe Customer + an active subscription + track
   how much of the $5 signup-credit pool remains so the paywall
   middleware (17.5) has something to gate against.

3. email_signin_tokens: a single-use, 15-minute TTL token table
   for the magic-link consume flow. Stored hashed (same sha256
   pattern keys.py uses for API keys) so a database leak doesn't
   hand attackers active sign-in tokens.

Backfill: every existing workspace gets plan_tier='free' +
free_credits_remaining_usd=5.00 — same starting credit a fresh
signup would land with. Existing users keep auth_provider='apikey'
since they were created via the existing /auth/signup path; only
new flows will use 'magic_link' / 'google_oauth'.

Indexes:
- workspaces.stripe_customer_id UNIQUE (one customer per workspace;
  the unique constraint also catches a duplicate-create bug loud).
- email_signin_tokens (email, created_at DESC) for the rate-limit
  query in 17.2 — "how many tokens have we issued for this email in
  the last hour."

Revision ID: 0030
Revises: 0029
Create Date: 2026-05-17
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---------- users ---------- #
    op.add_column(
        "users",
        sa.Column(
            "email_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "auth_provider",
            sa.String(length=16),
            nullable=False,
            server_default="apikey",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "google_user_id",
            sa.String(),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_users_google_user_id",
        "users",
        ["google_user_id"],
        unique=True,
        postgresql_where=sa.text("google_user_id IS NOT NULL"),
    )

    # ---------- workspaces ---------- #
    op.add_column(
        "workspaces",
        sa.Column(
            "stripe_customer_id",
            sa.String(),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_workspaces_stripe_customer_id",
        "workspaces",
        ["stripe_customer_id"],
        unique=True,
        postgresql_where=sa.text("stripe_customer_id IS NOT NULL"),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "stripe_subscription_id",
            sa.String(),
            nullable=True,
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "plan_tier",
            sa.String(length=16),
            nullable=False,
            server_default="free",
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "free_credits_remaining_usd",
            sa.Numeric(12, 6),
            nullable=False,
            server_default="5.00",
        ),
    )
    # Belt-and-suspenders backfill. Postgres applies the
    # server_default during ADD COLUMN for new rows + existing
    # rows when the default is a literal — but if a future
    # migration drops the default before relying on the column
    # being populated, the explicit UPDATE keeps things safe.
    op.execute(
        "UPDATE workspaces SET plan_tier = 'free' WHERE plan_tier IS NULL"
    )
    op.execute(
        "UPDATE workspaces "
        "SET free_credits_remaining_usd = 5.00 "
        "WHERE free_credits_remaining_usd IS NULL"
    )

    # ---------- email_signin_tokens ---------- #
    op.create_table(
        "email_signin_tokens",
        sa.Column("token_hash", sa.String(length=128), primary_key=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
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
    )
    op.create_index(
        "ix_email_signin_tokens_email_created",
        "email_signin_tokens",
        ["email", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_email_signin_tokens_email_created",
        table_name="email_signin_tokens",
    )
    op.drop_table("email_signin_tokens")
    op.drop_column("workspaces", "free_credits_remaining_usd")
    op.drop_column("workspaces", "plan_tier")
    op.drop_column("workspaces", "stripe_subscription_id")
    op.drop_index(
        "ix_workspaces_stripe_customer_id", table_name="workspaces"
    )
    op.drop_column("workspaces", "stripe_customer_id")
    op.drop_index("ix_users_google_user_id", table_name="users")
    op.drop_column("users", "google_user_id")
    op.drop_column("users", "auth_provider")
    op.drop_column("users", "email_verified")
