"""delivery channels (spec §18.4 — milestone M29)

Revision ID: h8c9d0e1f2a3
Revises: g7b8c9d0e1f2
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "h8c9d0e1f2a3"
down_revision: str | None = "g7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # per-channel send ledger: {channel: {ok, error, at}}
    op.add_column("deliveries", sa.Column("external", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("deliveries", "external")
