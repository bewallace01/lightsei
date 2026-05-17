"""sensitivity_level on agents + runs (Phase 16.1)

Schema backbone for Phase 16 (trust zones). Adds `sensitivity_level`
to both `agents` (the configuration knob) and `runs` (denormalized at
run-create time so historical analytics over verdicts / cost / events
by zone don't have to JOIN agents and lose correctness when the user
relabels an agent later).

Vocabulary: 'public' | 'internal' | 'sensitive' | 'pii'. The string
is intentionally not a Postgres enum so adding a future level
('regulated', 'export-controlled', etc.) is a code-only change.
Validation lives in `backend/models.py:_VALID_SENSITIVITY_LEVELS` +
the SDK/endpoint layer; the DB constraint is just NOT NULL.

Default 'internal': sensible middle of the road for existing workspaces
that haven't thought about zones yet. The new column is NOT NULL with
a server_default so fresh INSERTs from old code paths land on
'internal' automatically; the backfill pass below covers the existing
rows that predate the migration.

Indexes deliberately omitted: 16.4's cross-zone dispatch enforcement
looks up by (workspace_id, name) on agents — already covered by the
PK — and by (run_id) on runs — already covered. The dashboard's /zones
page (16.6) does scan by (workspace_id, sensitivity_level) on agents
but the table is small (≤ a few dozen rows per workspace) so a seq
scan is fine until we measure otherwise.

Revision ID: 0027
Revises: 0026
Create Date: 2026-05-17
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: Union[str, None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # agents: NOT NULL with default 'internal'. Postgres backfills
    # existing rows with the server_default in the ADD COLUMN itself
    # since the default is a literal, so no separate UPDATE pass is
    # needed — but we run one anyway as belt-and-suspenders in case
    # any future migration drops the default before relying on the
    # column being populated.
    op.add_column(
        "agents",
        sa.Column(
            "sensitivity_level",
            sa.String(length=16),
            nullable=False,
            server_default="internal",
        ),
    )
    op.execute(
        "UPDATE agents SET sensitivity_level = 'internal' "
        "WHERE sensitivity_level IS NULL OR sensitivity_level = ''"
    )

    op.add_column(
        "runs",
        sa.Column(
            "sensitivity_level",
            sa.String(length=16),
            nullable=False,
            server_default="internal",
        ),
    )
    op.execute(
        "UPDATE runs SET sensitivity_level = 'internal' "
        "WHERE sensitivity_level IS NULL OR sensitivity_level = ''"
    )


def downgrade() -> None:
    op.drop_column("runs", "sensitivity_level")
    op.drop_column("agents", "sensitivity_level")
