"""Phase 30.3: workspace-wide team conversations (Polaris-routed).

Adds two tables that parallel the per-agent threads / thread_messages
pair, but workspace-scoped instead of agent-scoped. A team_conversation
is one chat channel that accepts user messages addressed to the whole
team; a team_message is one row in that conversation.

Three message roles:

  "user"      The operator's message.
  "router"    The Polaris routing decision: a synthetic row written
              before the assistant rows. content holds a short human
              explanation; routed_agents holds the structured list
              of agent names + per-agent reasoning. status is always
              "completed" on router rows.
  "assistant" One row per agent the router picked. Each row starts
              "pending" with the agent_name set; the deployed agent's
              claim loop fills it in (same pattern as
              thread_messages, with agent_name now telling each
              agent which rows are theirs).

routed_agents is JSONB so the router decision is queryable + the
exact reasoning survives for the operator-visible attribution row.

Index on (conversation_id, created_at) for the chat-pane reader;
partial index on assistant rows where status='pending' for the
per-agent claim loop's hot path.

Revision ID: 0047
Revises: 0046
Create Date: 2026-05-30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "0047"
down_revision: Union[str, None] = "0046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "team_conversations",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String,
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String, nullable=False),
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
    op.create_index(
        "ix_team_conversations_ws_updated",
        "team_conversations",
        ["workspace_id", sa.text("updated_at DESC")],
    )

    op.create_table(
        "team_messages",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String,
            sa.ForeignKey("team_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column(
            "content",
            sa.Text,
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'completed'"),
        ),
        # Set on assistant rows so each agent's claim loop can pick
        # up its own row + so the chat-pane reader can attribute
        # the reply. NULL on user + router rows.
        sa.Column("agent_name", sa.String, nullable=True),
        # Set on router rows: the structured Polaris decision the
        # operator-visible router row was rendered from. Shape:
        #   {"agents": [{"name": "argus", "reason": "..."}, ...]}
        # NULL on user + assistant rows.
        sa.Column("routed_agents", JSONB(), nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_team_messages_conv_created",
        "team_messages",
        ["conversation_id", "created_at"],
    )
    # Claim hot path: oldest pending assistant row for a given
    # (workspace via conversation, agent).
    op.create_index(
        "ix_team_messages_pending_assistant",
        "team_messages",
        ["agent_name", "created_at"],
        postgresql_where=sa.text("status = 'pending' AND role = 'assistant'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_team_messages_pending_assistant",
        table_name="team_messages",
    )
    op.drop_index(
        "ix_team_messages_conv_created",
        table_name="team_messages",
    )
    op.drop_table("team_messages")
    op.drop_index(
        "ix_team_conversations_ws_updated",
        table_name="team_conversations",
    )
    op.drop_table("team_conversations")
