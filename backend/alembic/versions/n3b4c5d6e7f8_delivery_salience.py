"""Delivery salience + truthful delivery record (spec §17.5/§18.4 — M42)

`seen_at` records the moment a human actually opened a delivery, so
"was this attended to" stops being an inference; `salience` holds the
§17.5 judge verdict for the ledger. Both nullable — an untouched install
is byte-identical.

Revision ID: n3b4c5d6e7f8
Revises: m2a3b4c5d6e7
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "n3b4c5d6e7f8"
down_revision: str | None = "m2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("deliveries", sa.Column("seen_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("deliveries", sa.Column("salience", JSONB(), nullable=True))
    # the salience pass scans unseen, delivered, non-superseded rows
    op.create_index(
        "ix_deliveries_unseen",
        "deliveries",
        ["seen_at", "tier"],
        postgresql_where=sa.text("seen_at IS NULL AND superseded_by IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_deliveries_unseen", table_name="deliveries")
    op.drop_column("deliveries", "salience")
    op.drop_column("deliveries", "seen_at")
