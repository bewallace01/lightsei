"""Phase 36.1: widget customization (name, color, greeting).

Lets an owner brand the embedded website chatbot: a custom display name,
an accent color, and a welcome greeting. All nullable; NULL = sensible
default (assistant name / indigo / no greeting).

Revision ID: 0055
Revises: 0054
Create Date: 2026-06-13
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0055"
down_revision: Union[str, None] = "0054"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("workspaces",
                  sa.Column("widget_title", sa.String(length=60), nullable=True))
    op.add_column("workspaces",
                  sa.Column("widget_accent_color", sa.String(length=9), nullable=True))
    op.add_column("workspaces",
                  sa.Column("widget_greeting", sa.String(length=280), nullable=True))


def downgrade() -> None:
    op.drop_column("workspaces", "widget_greeting")
    op.drop_column("workspaces", "widget_accent_color")
    op.drop_column("workspaces", "widget_title")
