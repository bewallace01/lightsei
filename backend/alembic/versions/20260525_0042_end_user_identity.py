"""Phase 25.1: end-user identity tables.

Adds the four tables that back the consumer-facing chat surface
spec'd in Phase 25:

- `end_users`: the people who buy from a Lightsei-using business.
  Distinct from `users` (operators). Same workspace can have many
  end users + many operators; they share no rows.
- `end_user_sessions`: parallel to operator `sessions`. Bearer
  token for the end-user `/c` surface and the widget identified
  path. CASCADE on end_user delete.
- `end_user_vendor_links`: composite-PK join table. An end user
  is "subscribed" to a workspace (vendor) as a customer. v1 only
  inserts via invite-code redemption (Phase 25.2 + 27.2).
- `end_user_signin_tokens`: same shape as operator
  `email_signin_tokens` from Phase 17. token_hash PK, sha256 of
  the plaintext that goes in the magic link.

Also extends `widget_conversations` with a nullable `end_user_id`
fk so Phase 25.4 can scope an authenticated widget conversation
to a specific end user. Existing `anon_user_id` stays for the
anonymous v1 conversations; null end_user_id + non-null
anon_user_id is the legacy / opt-out path.

No backfill needed: no end-user rows exist before this migration.
Existing widget_conversations rows get NULL end_user_id (their
anon_user_id stays set), which the 25.4 query path treats as
anonymous, matching today's behavior.

Revision ID: 0042
Revises: 0041
Create Date: 2026-05-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0042"
down_revision: Union[str, None] = "0041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---------- end_users ---------- #

    op.create_table(
        "end_users",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("email", sa.String, nullable=False, unique=True),
        sa.Column("display_name", sa.String(length=128), nullable=True),
        sa.Column(
            "email_verified",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        # 'magic_link' in v1. Apple / Google OAuth deferred to 25B.
        sa.Column(
            "auth_provider",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'magic_link'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ---------- end_user_sessions ---------- #

    op.create_table(
        "end_user_sessions",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column(
            "end_user_id",
            sa.String,
            sa.ForeignKey("end_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String, nullable=False, unique=True),
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
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_end_user_sessions_end_user",
        "end_user_sessions",
        ["end_user_id"],
    )

    # ---------- end_user_vendor_links ---------- #

    op.create_table(
        "end_user_vendor_links",
        sa.Column(
            "end_user_id",
            sa.String,
            sa.ForeignKey("end_users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "workspace_id",
            sa.String,
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # 'invite_code' in v1; reserved values 'direct_invite' and
        # 'public_discovery' park to Phase 27B.
        sa.Column(
            "linked_via",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'invite_code'"),
        ),
        sa.Column(
            "linked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Soft-revoke pointer (Phase 27.2). Past conversations stay
        # accessible to the end user; no new messages can be sent.
        sa.Column(
            "removed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    # Per-workspace roster lookup. The PK already covers per-end-user.
    op.create_index(
        "ix_end_user_vendor_links_workspace",
        "end_user_vendor_links",
        ["workspace_id", "linked_at"],
    )

    # ---------- end_user_signin_tokens ---------- #

    op.create_table(
        "end_user_signin_tokens",
        sa.Column("token_hash", sa.String(length=128), primary_key=True),
        # NULL until the consume path either matches an existing
        # end_user or creates one. The request path stores the email
        # so the rate-limit query can scope by it, and so the consume
        # path can find-or-create on a known address.
        sa.Column("email", sa.String, nullable=False),
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
        # Optional invite code carried through the magic-link round
        # trip. When present + valid on consume, the 25.2 flow inserts
        # an end_user_vendor_links row in the same transaction.
        sa.Column(
            "vendor_invite_code",
            sa.String(length=64),
            nullable=True,
        ),
    )
    # Rate-limit + active-token probe scan, same shape as operator
    # email_signin_tokens index.
    op.create_index(
        "ix_end_user_signin_tokens_email_created",
        "end_user_signin_tokens",
        ["email", sa.text("created_at DESC")],
    )

    # ---------- widget_conversations.end_user_id ---------- #

    op.add_column(
        "widget_conversations",
        sa.Column(
            "end_user_id",
            sa.String,
            sa.ForeignKey("end_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    # Identified-end-user lookup: "show me this end user's
    # conversations on this vendor." Partial index keeps it tight
    # since most v1 conversations are still anonymous.
    op.create_index(
        "ix_widget_conversations_workspace_end_user",
        "widget_conversations",
        ["workspace_id", "end_user_id"],
        postgresql_where=sa.text("end_user_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_widget_conversations_workspace_end_user",
        table_name="widget_conversations",
    )
    op.drop_column("widget_conversations", "end_user_id")

    op.drop_index(
        "ix_end_user_signin_tokens_email_created",
        table_name="end_user_signin_tokens",
    )
    op.drop_table("end_user_signin_tokens")

    op.drop_index(
        "ix_end_user_vendor_links_workspace",
        table_name="end_user_vendor_links",
    )
    op.drop_table("end_user_vendor_links")

    op.drop_index(
        "ix_end_user_sessions_end_user",
        table_name="end_user_sessions",
    )
    op.drop_table("end_user_sessions")

    op.drop_table("end_users")
