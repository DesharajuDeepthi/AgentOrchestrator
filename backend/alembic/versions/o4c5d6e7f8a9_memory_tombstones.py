"""Durable forgetting — memory tombstones (spec §16.1 — M44)

FORGET leaves a metadata + normalized-text-hash trace (optionally an
embedding copy, used only for suppression) so the §16.2 admission gate
can refuse to re-learn what the user removed. ERASE and purge still
write nothing. New empty table — an untouched install is byte-identical.

Revision ID: o4c5d6e7f8a9
Revises: n3b4c5d6e7f8
"""

import pgvector.sqlalchemy
import sqlalchemy as sa

from alembic import op

revision: str = "o4c5d6e7f8a9"
down_revision: str | None = "n3b4c5d6e7f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_tombstones",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("project_key", sa.String(128), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("importance", sa.Integer(), nullable=False),
        sa.Column("age_days", sa.Float(), nullable=False),
        sa.Column("access_count", sa.Integer(), nullable=False),
        sa.Column("pinned", sa.Boolean(), nullable=False),
        sa.Column("text_hash", sa.String(64), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(), nullable=True),
        sa.Column("model_key", sa.String(255), nullable=True),
        sa.Column(
            "forgotten_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("suppressed_count", sa.Integer(), nullable=False),
        sa.Column("last_suppressed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("memory_tombstones_hash_idx", "memory_tombstones", ["text_hash"])
    op.create_index("memory_tombstones_owner_idx", "memory_tombstones", ["user_id", "scope"])


def downgrade() -> None:
    op.drop_index("memory_tombstones_owner_idx", table_name="memory_tombstones")
    op.drop_index("memory_tombstones_hash_idx", table_name="memory_tombstones")
    op.drop_table("memory_tombstones")
