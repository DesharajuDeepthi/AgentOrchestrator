"""Fire/hold decision record on ambient_events (spec §17.3).

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ambient_events", sa.Column("decision", JSONB))


def downgrade() -> None:
    op.drop_column("ambient_events", "decision")
