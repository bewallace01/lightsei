"""validator_configs: accept filename-keyed doc_hashes for polaris.plan

Phase 10.4 changed the Polaris bot so doc_hashes is keyed by filename
(e.g. {"MEMORY.md": "...", "TASKS.md": "..."}) instead of the original
fixed keys ("memory_md", "tasks_md"). The Phase 8 schema_strict validator
config was never updated to match, so any workspace created after Phase
10.4 would reject every polaris.plan emit with:

    schema_strict/required -- 'tasks_md' is a required property

The prod row was patched directly during the Phase 10.6 demo (commit
dc449e5). This migration backfills the fix for every other workspace
whose schema_strict config still carries the old shape, and ensures
newly-created workspaces that run setup_validators.py get the right
schema from now on (setup_validators.py was updated in the same commit
as this migration).

Before:
  doc_hashes: {type: object, required: [memory_md, tasks_md],
               properties: {memory_md: {type: string},
                            tasks_md:  {type: string}}}

After:
  doc_hashes: {type: object, minProperties: 1,
               additionalProperties: {type: string}}

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

_NEW_DOC_HASHES = (
    '{"type":"object","minProperties":1,'
    '"additionalProperties":{"type":"string"}}'
)

_OLD_DOC_HASHES = (
    '{"type":"object",'
    '"properties":{"memory_md":{"type":"string"},"tasks_md":{"type":"string"}},'
    '"required":["memory_md","tasks_md"]}'
)


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE validator_configs
            SET
                config     = jsonb_set(config,
                                       '{schema,properties,doc_hashes}',
                                       CAST(:new_schema AS jsonb)),
                updated_at = NOW()
            WHERE event_kind      = 'polaris.plan'
              AND validator_name  = 'schema_strict'
            """
        ),
        {"new_schema": _NEW_DOC_HASHES},
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE validator_configs
            SET
                config     = jsonb_set(config,
                                       '{schema,properties,doc_hashes}',
                                       CAST(:old_schema AS jsonb)),
                updated_at = NOW()
            WHERE event_kind      = 'polaris.plan'
              AND validator_name  = 'schema_strict'
            """
        ),
        {"old_schema": _OLD_DOC_HASHES},
    )
