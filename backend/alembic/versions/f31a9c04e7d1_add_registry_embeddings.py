"""add retrieval embedding columns to tools/skills/sub_agents (spec §7.4)

Revision ID: f31a9c04e7d1
Revises: d2a378047698
Create Date: 2026-08-06 22:05:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f31a9c04e7d1"
down_revision: str | None = "d2a378047698"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("tools", "skills", "sub_agents")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table, sa.Column("embedding", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
        )
        op.add_column(table, sa.Column("embedding_hash", sa.Text(), nullable=True))


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "embedding_hash")
        op.drop_column(table, "embedding")
