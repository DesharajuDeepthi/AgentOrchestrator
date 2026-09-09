"""A2A substrate (spec §19.2 — milestone M37, dark by default)

Revision ID: m2a3b4c5d6e7
Revises: k1f2a3b4c5d6
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "m2a3b4c5d6e7"
down_revision: str | None = "k1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "remote_agents",
        sa.Column("card_url", sa.Text(), nullable=False),
        sa.Column("card", JSONB, nullable=True),
        sa.Column("card_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auth_schemes", JSONB, nullable=True),
        # write-only credential store (spec §19.3) — never serialized outward
        sa.Column("credentials", JSONB, nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_remote_agents_name", "remote_agents", ["name"])
    op.create_table(
        "a2a_tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "remote_agent_id",
            UUID(as_uuid=True),
            sa.ForeignKey("remote_agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("call_key", sa.String(length=64), nullable=True),
        sa.Column("remote_task_id", sa.String(length=255), nullable=True),
        sa.Column("context_id", sa.String(length=255), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="submitted"),
        sa.Column("question", sa.Text(), nullable=True),
        sa.Column("result", JSONB, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("parked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_a2a_tasks_call_key", "a2a_tasks", ["call_key"])
    op.create_index("ix_a2a_tasks_agent_state", "a2a_tasks", ["remote_agent_id", "state"])
    op.add_column("tools", sa.Column("remote_agent_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_tools_remote_agent_id", "tools", "remote_agents", ["remote_agent_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_tools_remote_agent_id", "tools", type_="foreignkey")
    op.drop_column("tools", "remote_agent_id")
    op.drop_table("a2a_tasks")
    op.drop_table("remote_agents")
