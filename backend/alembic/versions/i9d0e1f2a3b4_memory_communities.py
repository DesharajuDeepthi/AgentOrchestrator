"""memory communities (spec §18.6 — milestone M31)

Revision ID: i9d0e1f2a3b4
Revises: h8c9d0e1f2a3
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "i9d0e1f2a3b4"
down_revision: str | None = "h8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_communities",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("label", sa.String(64), nullable=False, unique=True),
        sa.Column("member_entity_ids", JSONB, nullable=True),
        sa.Column("member_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("signature", sa.String(64), nullable=False, server_default=""),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("memory_communities")
