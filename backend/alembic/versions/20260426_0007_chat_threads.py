"""chat threads + messages

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "threads",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column(
            "workspace_id",
            sa.String(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_name", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "idx_threads_ws_agent",
        "threads",
        ["workspace_id", "agent_name", sa.text("updated_at DESC")],
    )

    op.create_table(
        "thread_messages",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column(
            "thread_id",
            sa.String(),
            sa.ForeignKey("threads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(), nullable=False),       # 'user' | 'assistant' | 'system'
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "status", sa.String(), nullable=False, server_default="completed"
        ),  # 'completed' | 'pending' | 'failed'
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_thread_messages_thread",
        "thread_messages",
        ["thread_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_thread_messages_thread", table_name="thread_messages")
    op.drop_table("thread_messages")
    op.drop_index("idx_threads_ws_agent", table_name="threads")
    op.drop_table("threads")
