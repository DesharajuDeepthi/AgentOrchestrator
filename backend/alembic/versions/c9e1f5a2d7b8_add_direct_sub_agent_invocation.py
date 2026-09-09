"""direct sub-agent invocation (spec §7.5): sub_agents.direct_exposure gate
+ runs.target_sub_agent_id for pinned runs

Revision ID: c9e1f5a2d7b8
Revises: b7c4d2e9f1a3
Create Date: 2026-08-09
"""

import sqlalchemy as sa

from alembic import op

revision: str = "c9e1f5a2d7b8"
down_revision: str | None = "b7c4d2e9f1a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sub_agents",
        sa.Column("direct_exposure", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("runs", sa.Column("target_sub_agent_id", sa.Uuid(), nullable=True))
    # static seed agents ship exposed (spec §3.4) — the toggle stays live
    op.execute("UPDATE sub_agents SET direct_exposure = TRUE WHERE source = 'static'")


def downgrade() -> None:
    op.drop_column("runs", "target_sub_agent_id")
    op.drop_column("sub_agents", "direct_exposure")
