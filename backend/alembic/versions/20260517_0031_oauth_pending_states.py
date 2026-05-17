"""Phase 17.3: oauth_pending_states — short-lived state + PKCE store.

OAuth 2.0 authorization-code flow with PKCE needs the server to
remember (a) the state value it generated for the redirect, (b) the
code_verifier that pairs with the challenge it sent to Google. Both
have to survive the user's hop out to Google's consent screen and
back, but only for the few seconds between /auth/google/start and
/auth/google/callback.

A small table beats a signed cookie here because we don't have any
cookie infra in the backend today and we're not going to grow one
just for this. Rows are tiny + we reap them in /callback (and any
left behind expire harmlessly — the next start always inserts a
fresh row).

Schema:
- state (PK): random URL-safe string, also sent to Google so the
  callback can match.
- code_verifier: the PKCE secret. Hashed challenge went to Google;
  verifier stays here until exchange.
- redirect_after: where the dashboard wanted to land after sign-in
  (so /auth/google/start can be called from any signed-out page).
- created_at, expires_at: 10-minute TTL.

Revision ID: 0031
Revises: 0030
Create Date: 2026-05-17
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: Union[str, None] = "0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "oauth_pending_states",
        sa.Column("state", sa.String(length=128), primary_key=True),
        sa.Column("code_verifier", sa.String(length=128), nullable=False),
        sa.Column("redirect_after", sa.String(length=512), nullable=True),
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
    )
    op.create_index(
        "ix_oauth_pending_states_expires_at",
        "oauth_pending_states",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_oauth_pending_states_expires_at",
        table_name="oauth_pending_states",
    )
    op.drop_table("oauth_pending_states")
