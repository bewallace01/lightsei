"""dispatch chain machinery (Phase 11.2)

The schema half of the dispatch story. The SDK in Phase 11.1 already
sends `dispatch_chain_id` on the wire (Pydantic silently dropped it
until now); 11.2 actually persists it, computes per-hop depth, holds
commands at an `approval_state` gate, and enforces per-agent dispatch
caps so a buggy bot can't fork-bomb.

Six schema changes plus one data step:

  1. commands.source_agent  varchar NULL
       Who dispatched the command. NULL when enqueued by a user
       (dashboard click) or by an off-platform integration like
       /webhooks/github. The constellation map's edges read from
       this column to draw dispatch lines between agents.

  2. commands.dispatch_chain_id  varchar NOT NULL
       Groups every command in a single cause-and-effect chain. Set
       to a fresh UUID at the chain's root; inherited from the
       parent command for descendants. Indexed so the dashboard's
       /dispatch view can look up "all commands in this chain" in
       a single index scan. Server default '00000000-...-000000'
       because Postgres NOT NULL needs a default for ALTER TABLE.

  3. commands.dispatch_depth  int NOT NULL DEFAULT 0
       Hops from the chain's root. parent.dispatch_depth + 1, capped
       at agent.max_dispatch_depth. Default 0 marks the root command.

  4. commands.approval_state  varchar(16) NOT NULL DEFAULT 'pending'
       Human-in-the-loop gate. claim_command only returns rows in
       {'approved', 'auto_approved'}. 'pending' commands sit safely
       until acted on; 'rejected' / 'expired' are terminal.

  5. commands.approved_by_user_id  varchar NULL FK users.id
     commands.approved_at         timestamptz NULL
       Audit trail for who clicked the approve button.

  6. command_auto_approval_rules
       (workspace_id, source_agent, target_agent, command_kind) PK
       with `mode in {auto_approve, require_human}`. Lookup at enqueue
       time fires before the command lands; matching rules flip
       approval_state to 'auto_approved' or pin it to 'pending' even
       under wildcards.

Plus two columns on `agents` for per-agent dispatch caps:

  agents.max_dispatch_depth   int NOT NULL DEFAULT 5
  agents.max_dispatch_per_day int NOT NULL DEFAULT 100

Data step: every existing command gets a fresh dispatch_chain_id so
back-history doesn't all collapse onto the sentinel default. Per-row
UUID via gen_random_uuid() (pgcrypto extension is on by default in
modern Postgres; if not, the existing app-level uuid generation
keeps working for new rows).

Revision ID: 0020
Revises: 0019
Create Date: 2026-05-02
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # commands columns ------------------------------------------------
    op.add_column(
        "commands",
        sa.Column("source_agent", sa.String(), nullable=True),
    )
    op.add_column(
        "commands",
        sa.Column(
            "dispatch_chain_id",
            sa.String(),
            nullable=False,
            server_default="00000000-0000-0000-0000-000000000000",
        ),
    )
    op.add_column(
        "commands",
        sa.Column(
            "dispatch_depth",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "commands",
        sa.Column(
            "approval_state",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "commands",
        sa.Column("approved_by_user_id", sa.String(), nullable=True),
    )
    op.add_column(
        "commands",
        sa.Column(
            "approved_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_commands_approved_by_user",
        "commands",
        "users",
        ["approved_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "idx_commands_chain", "commands", ["dispatch_chain_id"]
    )
    op.create_index(
        "idx_commands_ws_source_recent",
        "commands",
        ["workspace_id", "source_agent", "created_at"],
    )

    # agents per-agent caps ------------------------------------------
    op.add_column(
        "agents",
        sa.Column(
            "max_dispatch_depth",
            sa.Integer(),
            nullable=False,
            server_default="5",
        ),
    )
    op.add_column(
        "agents",
        sa.Column(
            "max_dispatch_per_day",
            sa.Integer(),
            nullable=False,
            server_default="100",
        ),
    )

    # auto_approval_rules table --------------------------------------
    op.create_table(
        "command_auto_approval_rules",
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("source_agent", sa.String(length=64), nullable=False),
        sa.Column("target_agent", sa.String(length=64), nullable=False),
        sa.Column("command_kind", sa.String(length=128), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint(
            "workspace_id", "source_agent", "target_agent", "command_kind"
        ),
    )

    # Existing commands: give each a fresh chain id so back-history
    # doesn't all live under the sentinel default. Existing approval
    # state stays 'pending' from the server_default, which means
    # historical rows can't be claimed without an explicit approval
    # — that's correct: a command from before this phase shouldn't
    # silently fire post-deploy.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # pgcrypto's gen_random_uuid() is available on Railway's
        # managed Postgres without an explicit CREATE EXTENSION.
        op.execute(
            """
            UPDATE commands
            SET dispatch_chain_id = gen_random_uuid()::text
            WHERE dispatch_chain_id =
              '00000000-0000-0000-0000-000000000000'
            """
        )


def downgrade() -> None:
    op.drop_table("command_auto_approval_rules")
    op.drop_column("agents", "max_dispatch_per_day")
    op.drop_column("agents", "max_dispatch_depth")
    op.drop_index("idx_commands_ws_source_recent", table_name="commands")
    op.drop_index("idx_commands_chain", table_name="commands")
    op.drop_constraint(
        "fk_commands_approved_by_user", "commands", type_="foreignkey"
    )
    op.drop_column("commands", "approved_at")
    op.drop_column("commands", "approved_by_user_id")
    op.drop_column("commands", "approval_state")
    op.drop_column("commands", "dispatch_depth")
    op.drop_column("commands", "dispatch_chain_id")
    op.drop_column("commands", "source_agent")
