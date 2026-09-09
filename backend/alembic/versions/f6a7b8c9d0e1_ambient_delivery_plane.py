"""ambient delivery plane (spec §17.5/§17.6 — milestone M23)

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # category policy ledger: append-only; the latest row per category wins,
    # history is the audit trail and the revert path (spec §17.6/§17.7)
    op.create_table(
        "ambient_policies",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("tier_override", sa.SmallInteger(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False, server_default="rule"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ambient_policies_cat_idx", "ambient_policies", ["category", "created_at"])
    # supersede-collapse key: pending items sharing it are superseded by the
    # newest arrival (spec §17.5)
    op.add_column("deliveries", sa.Column("skey", sa.String(255), nullable=True))
    op.create_index("deliveries_skey_idx", "deliveries", ["skey"])
    # presence: when the current state began — user_returned carries away_s
    op.add_column(
        "user_presence", sa.Column("state_since", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("user_presence", "state_since")
    op.drop_index("deliveries_skey_idx", table_name="deliveries")
    op.drop_column("deliveries", "skey")
    op.drop_index("ambient_policies_cat_idx", table_name="ambient_policies")
    op.drop_table("ambient_policies")
