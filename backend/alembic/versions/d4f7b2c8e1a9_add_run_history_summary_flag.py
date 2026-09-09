"""runs.include_history_summary — §7.5 opt-in history summary on direct runs

Revision ID: d4f7b2c8e1a9
Revises: c9e1f5a2d7b8
Create Date: 2026-08-09
"""

import sqlalchemy as sa

from alembic import op

revision: str = "d4f7b2c8e1a9"
down_revision: str | None = "c9e1f5a2d7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "include_history_summary", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade() -> None:
    op.drop_column("runs", "include_history_summary")
