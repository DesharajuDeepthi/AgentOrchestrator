"""Tombstone distinctive-token hashes (spec §16.1 — M44 hybrid gate)

Live calibration measured real paraphrase pairs at cosine 0.876 and
0.847 — a single threshold cannot separate "same fact restated" from
"same topic, different fact". The tombstone gains hashes of the text's
distinctive tokens (same privacy tier and dictionary-attack caveat as
the text hash) so the gate can demand a shared anchor in the gray band.

Revision ID: p5d6e7f8a9b0
Revises: o4c5d6e7f8a9
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "p5d6e7f8a9b0"
down_revision: str | None = "o4c5d6e7f8a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "memory_tombstones",
        sa.Column("token_hashes", JSONB(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("memory_tombstones", "token_hashes")
