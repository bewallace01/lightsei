"""Backfill built-in persona capabilities.

Bundled personas were provisioned with empty capability lists, but their
runtime code needs internet and dispatch grants to call Anthropic, probe
websites, and alert Hermes.

Revision ID: 0057
Revises: 0056
Create Date: 2026-06-16
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0057"
down_revision: Union[str, None] = "0056"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _merge_caps_sql(names: tuple[str, ...], caps_json: str) -> str:
    names_sql = ", ".join(f"'{name}'" for name in names)
    return f"""
        UPDATE agents
           SET capabilities = (
                SELECT COALESCE(jsonb_agg(cap), '[]'::jsonb)
                  FROM (
                        SELECT DISTINCT cap
                          FROM jsonb_array_elements_text(
                               COALESCE(capabilities, '[]'::jsonb)
                               || '{caps_json}'::jsonb
                          ) AS cap
                  ) AS merged
           )
         WHERE name IN ({names_sql})
    """


def upgrade() -> None:
    op.execute(_merge_caps_sql(
        ("website", "marketing", "bi", "inbox"),
        '["internet", "send_command"]',
    ))
    op.execute(_merge_caps_sql(
        ("lead", "reputation"),
        '["send_command"]',
    ))


def downgrade() -> None:
    # Data backfill only. Do not remove capabilities an operator may now rely on.
    pass
