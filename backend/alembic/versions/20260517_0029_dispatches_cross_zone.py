"""dispatches_cross_zone on agents (Phase 16.4)

Adds the per-agent opt-in flag for cross-zone dispatch. The framework
default is same-zone-only: an agent's `send_command` to a different
sensitivity zone is refused unless `dispatches_cross_zone=True` on
the SOURCE agent. Property of the agent (not the dispatch rule) so
it's a property of "this bot is trusted to cross zones" rather than
"this specific call slipped through approval."

Default False: every existing agent stays in the safer same-zone-only
posture until the user explicitly opts an agent in. Phase 11.2's
auto-approval rules continue to apply on top — cross-zone-enabled
does NOT mean auto-approved; a cross-zone command can still be
rejected by a pending-approval rule.

Revision ID: 0029
Revises: 0028
Create Date: 2026-05-17
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "dispatches_cross_zone",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Belt-and-suspenders: Postgres backfills with the server_default
    # on ADD COLUMN, but explicit UPDATE survives any future migration
    # that drops the default before relying on the column.
    op.execute(
        "UPDATE agents SET dispatches_cross_zone = false "
        "WHERE dispatches_cross_zone IS NULL"
    )


def downgrade() -> None:
    op.drop_column("agents", "dispatches_cross_zone")
