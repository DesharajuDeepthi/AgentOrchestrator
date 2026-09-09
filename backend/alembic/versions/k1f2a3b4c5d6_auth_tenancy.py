"""auth & tenancy (spec §18.8 — milestone M34, dark by default)

Revision ID: k1f2a3b4c5d6
Revises: j0e1f2a3b4c5
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "k1f2a3b4c5d6"
down_revision: str | None = "j0e1f2a3b4c5"
branch_labels = None
depends_on = None

_WORK_TABLES = [
    "conversations",
    "runs",
    "memories",
    "routines",
    "standing_intents",
    "deliveries",
    "ambient_policies",
    "user_presence",
]


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(512), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="member"),
        sa.Column("prefs", JSONB, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(128), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    for table in _WORK_TABLES:
        op.add_column(table, sa.Column("user_id", UUID(as_uuid=True), nullable=True))
        op.create_index(f"{table}_user_idx", table, ["user_id"])


def downgrade() -> None:
    for table in reversed(_WORK_TABLES):
        op.drop_index(f"{table}_user_idx", table_name=table)
        op.drop_column(table, "user_id")
    op.drop_table("auth_sessions")
    op.drop_table("users")
