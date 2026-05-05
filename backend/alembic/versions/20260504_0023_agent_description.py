"""agents.description — short freeform "what this bot does"

Used by the /agents roster to show one-line descriptions next to each
bot, and auto-populated from the LLM's rationale when bots are
generated via 12B's /workspaces/me/agents/generate. Hand-deployed
agents start null; the user can write one via PATCH.

Revision ID: 0023
Revises: 0022
Create Date: 2026-05-04
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("description", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agents", "description")
