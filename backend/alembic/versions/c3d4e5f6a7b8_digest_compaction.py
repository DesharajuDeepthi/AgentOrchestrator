"""run_digests compaction columns (spec §16.7): kind, covers range, nullable run_id.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-20
"""

import sqlalchemy as sa

from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("run_digests", "run_id", nullable=True)
    op.add_column(
        "run_digests",
        sa.Column("kind", sa.String(16), nullable=False, server_default="run"),
    )
    op.add_column("run_digests", sa.Column("covers_from", sa.DateTime(timezone=True)))
    op.add_column("run_digests", sa.Column("covers_to", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("run_digests", "covers_to")
    op.drop_column("run_digests", "covers_from")
    op.drop_column("run_digests", "kind")
    op.execute("DELETE FROM run_digests WHERE run_id IS NULL")
    op.alter_column("run_digests", "run_id", nullable=False)
