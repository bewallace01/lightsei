"""agents.tick_interval_s — per-agent schedule override

Adds a nullable integer column on `agents` for cron-style bots (Polaris and
future schedulers) to read at tick time. Null means "use the bot's env
default" (POLARIS_POLL_S, etc.). Otherwise it's the seconds between ticks.

Bots that don't use a tick loop (atlas, hermes — they're reactive, claiming
commands) simply ignore the column.

Revision ID: 0022
Revises: 0021
Create Date: 2026-05-04
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("tick_interval_s", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agents", "tick_interval_s")
