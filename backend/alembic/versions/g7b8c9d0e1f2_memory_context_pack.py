"""memory context pack (spec §18.2 — milestone M27)

Revision ID: g7b8c9d0e1f2
Revises: f6a7b8c9d0e1
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "g7b8c9d0e1f2"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 'project' memory scope: the key partitions project-scoped rows
    op.add_column("memories", sa.Column("project_key", sa.String(128), nullable=True))
    op.create_index("memories_project_idx", "memories", ["project_key"])
    op.add_column("conversations", sa.Column("project_key", sa.String(128), nullable=True))
    # routine cross-fire continuity: opt-in persistent conversation
    op.add_column(
        "routines",
        sa.Column("include_memories", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column("routines", sa.Column("conversation_id", UUID(as_uuid=True), nullable=True))


def downgrade() -> None:
    op.drop_column("routines", "conversation_id")
    op.drop_column("routines", "include_memories")
    op.drop_column("conversations", "project_key")
    op.drop_index("memories_project_idx", table_name="memories")
    op.drop_column("memories", "project_key")
