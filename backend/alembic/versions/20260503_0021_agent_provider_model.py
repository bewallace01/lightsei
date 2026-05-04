"""agents.provider + agents.model (Phase 12.1)

Adds nullable `provider` and `model` columns to the `agents` table so
each agent can pin its preferred LLM provider + model id. When null
(the default for existing rows), the constellation map and cost panel
fall back to whatever the SDK reported on the latest `llm_call_completed`
event — same behavior as before this migration. When set, the dashboard
+ a future scheduling layer can route the agent's calls deliberately
(e.g., swap atlas from Anthropic Haiku to Gemini Flash without code
changes).

`provider` is constrained at the API layer (Pydantic) to the small enum
{openai, anthropic, google, groq, xai, cohere}; we don't enforce that
at the DB level so a future adapter can land without a schema migration.

Revision ID: 0021
Revises: 0020
Create Date: 2026-05-03
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("provider", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "agents",
        sa.Column("model", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agents", "model")
    op.drop_column("agents", "provider")
