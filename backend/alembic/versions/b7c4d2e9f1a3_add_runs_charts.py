"""add runs.charts — render_chart tool specs, formatter-independent (spec §3.3)

Revision ID: b7c4d2e9f1a3
Revises: a8b3c9d1e2f4
Create Date: 2026-08-08
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b7c4d2e9f1a3"
down_revision: str | None = "a8b3c9d1e2f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("charts", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("runs", "charts")
