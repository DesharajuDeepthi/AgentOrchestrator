"""add skills.max_tool_iterations (spec §3.3 per-skill loop budget)

Revision ID: a8b3c9d1e2f4
Revises: f31a9c04e7d1
Create Date: 2026-08-07 05:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a8b3c9d1e2f4"
down_revision: str | None = "f31a9c04e7d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("skills", sa.Column("max_tool_iterations", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("skills", "max_tool_iterations")
