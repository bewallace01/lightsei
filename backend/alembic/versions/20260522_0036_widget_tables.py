"""Phase 21.1: widget chat schema.

Three new tables drive the customer-facing chat widget + operator
inbox (Phase 21):

- `widget_conversations`: one per widget session. Status machine
  (open / escalated / operator_owned / resolved). Snapshots the
  customer-facing bot name at conversation-start so renaming the
  bot later doesn't break old threads.
- `widget_messages`: chat history. Role enum (user / bot /
  operator / system). System rows are events like "Operator
  joined" / "Bot escalated".
- `widget_escalations`: state machine for "this conversation
  needs operator attention". Same row gains a `suggested_fix`
  jsonb in Phase 21.9 when Polaris's incident-response extension
  ships.

Three new columns on `workspaces`:

- `customer_facing_agent_name`: which bot answers widget messages
  for this workspace. Operator picks via the 21.7 settings page.
  Stored as a plain string (the agents table uses a composite PK
  `(workspace_id, name)`, so a real FK doesn't fit cleanly).
- `widget_public_id`: short URL-safe random string used in the
  widget snippet. Distinct from `workspaces.id` so we can rotate
  it without breaking customer-side URL stability promises.
  Unique across workspaces.
- `allowed_widget_origins`: jsonb array of HTTPS origins the
  widget will accept POST /widget/{public_id}/messages from. The
  21.2 endpoint enforces this against the request's Origin header.

Why a `customer_facing_agent_name` snapshot on
`widget_conversations` AND a current pointer on `workspaces`:
the workspace column is "who answers new conversations right
now"; the conversation column is "who was answering this thread
when it started." Operators can swap the customer-facing bot
without rewriting history.

Revision ID: 0036
Revises: 0035
Create Date: 2026-05-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "0036"
down_revision: Union[str, None] = "0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---------- Workspace columns ---------- #

    op.add_column(
        "workspaces",
        sa.Column(
            "customer_facing_agent_name",
            sa.String(length=128),
            nullable=True,
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "widget_public_id",
            sa.String(length=32),
            nullable=True,
            unique=True,
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "allowed_widget_origins",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    # Unique index for the public id so the constraint shows up in
    # pg_indexes (some Alembic versions don't emit one for column-level
    # unique=True alone).
    op.create_index(
        "ix_workspaces_widget_public_id",
        "workspaces",
        ["widget_public_id"],
        unique=True,
    )

    # ---------- widget_conversations ---------- #

    op.create_table(
        "widget_conversations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String,
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Bot name snapshot at conversation-start. Plain string;
        # agents use a composite PK so a real FK doesn't fit. App-side
        # code resolves to the live agent row when needed.
        sa.Column(
            "customer_facing_agent_name",
            sa.String(length=128),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default=sa.text("'open'"),
        ),
        # Lightsei-side opaque user id (anonymous-only in v1; widget
        # iframe localStorage carries it forward across page loads
        # for the same end user on the same site). NOT a verified
        # identity — Phase 21B adds signed-token passthrough.
        sa.Column(
            "anon_user_id",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "last_message_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    # Inbox list query: "give me this workspace's conversations
    # filtered by status, most-recently-active first." Covers the
    # /inbox + filter combo without a separate scan.
    op.create_index(
        "ix_widget_conversations_workspace_status_active",
        "widget_conversations",
        ["workspace_id", "status", sa.text("last_message_at DESC")],
    )
    # Anon-user lookup for "did this end user have a previous
    # conversation on this workspace?" Optional UX nicety; cheap.
    op.create_index(
        "ix_widget_conversations_workspace_anon_user",
        "widget_conversations",
        ["workspace_id", "anon_user_id"],
        postgresql_where=sa.text("anon_user_id IS NOT NULL"),
    )

    # ---------- widget_messages ---------- #

    op.create_table(
        "widget_messages",
        sa.Column(
            "id",
            sa.BigInteger,
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "conversation_id",
            sa.String(length=36),
            sa.ForeignKey("widget_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(length=16), nullable=False),
        # Text body of the message. Cap is app-side; the column is
        # Text so a long bot answer + tool-call trace can fit.
        sa.Column("text", sa.Text, nullable=False),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
    )
    # Primary thread-render query: "messages in this conversation,
    # oldest first." Also drives the widget's poll-since-cursor
    # endpoint via id > since.
    op.create_index(
        "ix_widget_messages_conversation_sent_at",
        "widget_messages",
        ["conversation_id", "sent_at"],
    )

    # ---------- widget_escalations ---------- #

    op.create_table(
        "widget_escalations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(length=36),
            sa.ForeignKey("widget_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Short keyword: 'bot_escalate_call' (explicit SDK call),
        # 'bot_crash' (handler raised), 'operator_requested' (operator
        # marked escalated from /inbox), 'low_confidence' (future
        # heuristic, parked to 21B). Free-form so adding new reasons
        # doesn't need a migration.
        sa.Column("reason", sa.String(length=64), nullable=False),
        # Context the bot wants to attach for the operator
        # (last_user_message, attempted_search, anything that makes
        # the escalation actionable). Free-form jsonb.
        sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        # Phase 21.9 adds the Polaris-suggested fix in this column.
        # Null until incident-response detection produces one.
        # Shape: {kind: 'system_prompt_addendum'|'add_faq_entry',
        # detail: <markdown or json>}.
        sa.Column("suggested_fix", JSONB, nullable=True),
        sa.Column(
            "escalated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "resolved_by_user_id",
            sa.String,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    # Polaris's pattern-detection query (21.9): "give me the last
    # N hours of unresolved escalations on this workspace, group
    # by reason + cluster on the conversation's last user message."
    op.create_index(
        "ix_widget_escalations_open_recent",
        "widget_escalations",
        [sa.text("escalated_at DESC")],
        postgresql_where=sa.text("resolved_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_widget_escalations_open_recent",
        table_name="widget_escalations",
    )
    op.drop_table("widget_escalations")

    op.drop_index(
        "ix_widget_messages_conversation_sent_at",
        table_name="widget_messages",
    )
    op.drop_table("widget_messages")

    op.drop_index(
        "ix_widget_conversations_workspace_anon_user",
        table_name="widget_conversations",
    )
    op.drop_index(
        "ix_widget_conversations_workspace_status_active",
        table_name="widget_conversations",
    )
    op.drop_table("widget_conversations")

    op.drop_index("ix_workspaces_widget_public_id", table_name="workspaces")
    op.drop_column("workspaces", "allowed_widget_origins")
    op.drop_column("workspaces", "widget_public_id")
    op.drop_column("workspaces", "customer_facing_agent_name")
