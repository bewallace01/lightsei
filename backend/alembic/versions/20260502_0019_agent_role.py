"""agents.role + auto-tag Polaris as orchestrator (Phase 11B.3)

Adds the `role` column on `agents` so the constellation map can
render each agent at the right radius around the orchestrator. Four
tiers, all matched on string equality:

  orchestrator  Polaris. Fixed at canvas center.
  executor      Atlas, future test/build/deploy roles. Inner ring (r=150).
  notifier      Hermes, future Slack/email/SMS roles. Outer ring (r=250).
  specialist    Future Argus / Vega / Sirius. Mid ring (r=200).

Server_default 'executor' so existing rows take a sensible value on
backfill — most agents in the wild are executors. The migration's
data step then re-tags any agent named 'polaris' to 'orchestrator'
in one pass; no other roles are inferred (the user labels them
explicitly later via a future PATCH /agents/{name} surface — for
Phase 11B.3 the visual map renders fine even if everyone except
Polaris stays 'executor' until then).

Revision ID: 0019
Revises: 0018
Create Date: 2026-05-02
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "role",
            sa.String(length=32),
            nullable=False,
            server_default="executor",
        ),
    )
    # Auto-tag any agent named exactly 'polaris' as the orchestrator
    # so the constellation centerpiece renders correctly out of the
    # box. Other agents stay 'executor' until the user relabels.
    op.execute(
        """
        UPDATE agents
        SET role = 'orchestrator'
        WHERE name = 'polaris'
        """
    )


def downgrade() -> None:
    op.drop_column("agents", "role")
