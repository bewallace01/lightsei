"""Phase 23.1: workspace_members join + per-session active workspace.

Two new schema pieces drive the multiple-workspaces-per-account
surface (Phase 23):

- `workspace_members`: composite-PK join table on
  `(user_id, workspace_id)`. Replaces the implicit
  `user.workspace_id` "one workspace per user" relationship. v1
  only inserts one row per workspace at create time
  (role='owner'); the many-to-many shape is built for Phase 23B's
  invite + accept flow.
- `sessions.active_workspace_id`: per-session pointer to the
  workspace the dashboard is currently showing. Lets two browser
  tabs hold two different workspaces open without stomping each
  other. Nullable so the migration's same-transaction backfill
  isn't a NOT NULL fight; populated immediately for every existing
  session row.

Backfill: every existing `users` row gets a single
`workspace_members` entry pointing at their current workspace_id
with role='owner'. Every existing `sessions` row gets
active_workspace_id set to the same value. Both writes use
ON CONFLICT DO NOTHING so a partially-run migration is safe to
re-run.

The legacy `users.workspace_id` column stays — Phase 23 doesn't
drop it. Existing endpoint code that resolves "the user's
workspace" continues to work via that column until 23.2 flips
`get_workspace_id` to read from `sessions.active_workspace_id`.
Dropping `users.workspace_id` parks to a later cleanup once
nothing reads it anymore.

Revision ID: 0040
Revises: 0039
Create Date: 2026-05-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0040"
down_revision: Union[str, None] = "0039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---------- workspace_members ---------- #

    op.create_table(
        "workspace_members",
        sa.Column(
            "user_id",
            sa.String,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "workspace_id",
            sa.String,
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # 'owner' or 'member'. Only 'owner' inserted in v1; the
        # 'member' value is reserved for Phase 23B's invite flow.
        sa.Column(
            "role",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'owner'"),
        ),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # Per-workspace roster (chronological joined-at). The PK
    # already covers per-user lookup.
    op.create_index(
        "ix_workspace_members_workspace",
        "workspace_members",
        ["workspace_id", "joined_at"],
    )

    # ---------- sessions.active_workspace_id ---------- #

    op.add_column(
        "sessions",
        sa.Column(
            "active_workspace_id",
            sa.String,
            sa.ForeignKey("workspaces.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    # Hot lookup: every session-authed request reads this column.
    op.create_index(
        "ix_sessions_active_workspace",
        "sessions",
        ["active_workspace_id"],
    )

    # ---------- Backfill existing data ---------- #

    # Every existing user becomes the owner of their existing
    # workspace. ON CONFLICT DO NOTHING makes the migration safe
    # to re-run partially (e.g. if it failed mid-flight previously).
    op.execute(
        """
        INSERT INTO workspace_members (user_id, workspace_id, role, joined_at)
        SELECT id, workspace_id, 'owner', COALESCE(created_at, now())
          FROM users
         WHERE workspace_id IS NOT NULL
        ON CONFLICT (user_id, workspace_id) DO NOTHING
        """
    )

    # Every existing session gets its active_workspace_id set to the
    # user's only workspace. Sessions whose user has been deleted
    # since (orphan rows) get NULL and will redirect through the
    # workspace picker on next request (added in 23.6).
    op.execute(
        """
        UPDATE sessions
           SET active_workspace_id = users.workspace_id
          FROM users
         WHERE sessions.user_id = users.id
           AND sessions.active_workspace_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_sessions_active_workspace", table_name="sessions",
    )
    op.drop_column("sessions", "active_workspace_id")

    op.drop_index(
        "ix_workspace_members_workspace", table_name="workspace_members",
    )
    op.drop_table("workspace_members")
