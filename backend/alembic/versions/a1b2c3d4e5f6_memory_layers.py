"""memory layer tables + pgvector extension (spec §16.1)

Revision ID: a1b2c3d4e5f6
Revises: d4f7b2c8e1a9
Create Date: 2026-08-20 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "d4f7b2c8e1a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.create_table(
        "memories",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column(
            "conversation_id",
            sa.Uuid(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("entity_key", sa.String(255), nullable=True),
        sa.Column("importance", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "valid_from",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("supersedes", sa.Uuid(), sa.ForeignKey("memories.id"), nullable=True),
        sa.Column("superseded_by", sa.Uuid(), sa.ForeignKey("memories.id"), nullable=True),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("step_id", sa.Uuid(), nullable=True),
        sa.Column(
            "last_accessed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("access_count", sa.Integer(), nullable=False),
        sa.Column("pinned", sa.Boolean(), nullable=False),
        sa.Column("half_life_days", sa.Float(), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column(
            "fts",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', text)", persisted=True),
            nullable=True,
        ),
    )
    op.create_index(
        "memories_current_scope_kind_idx",
        "memories",
        ["scope", "kind"],
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index("memories_fts_idx", "memories", ["fts"], postgresql_using="gin")
    op.create_index("memories_conversation_idx", "memories", ["conversation_id"])

    op.create_table(
        "memory_embeddings",
        sa.Column("ref_id", sa.Uuid(), primary_key=True),
        sa.Column("table_ref", sa.String(24), primary_key=True),
        sa.Column("model_key", sa.String(255), primary_key=True),
        sa.Column("embedding", Vector(None), nullable=False),
        sa.Column(
            "embedded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("memory_embeddings_model_idx", "memory_embeddings", ["model_key"])

    op.create_table(
        "memory_entities",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=True),
        sa.Column("aliases", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    op.create_table(
        "memory_entity_links",
        sa.Column(
            "memory_id",
            sa.Uuid(),
            sa.ForeignKey("memories.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "entity_id",
            sa.Uuid(),
            sa.ForeignKey("memory_entities.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )

    op.create_table(
        "run_digests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "conversation_id",
            sa.Uuid(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("signals", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "fts",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', text)", persisted=True),
            nullable=True,
        ),
    )
    op.create_index("run_digests_fts_idx", "run_digests", ["fts"], postgresql_using="gin")

    op.create_table(
        "conversation_rollups",
        sa.Column(
            "conversation_id",
            sa.Uuid(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("runs_covered", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    op.create_table(
        "plan_exemplars",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("task_text", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("trace", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("votes", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "fts",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', task_text)", persisted=True),
            nullable=True,
        ),
    )
    op.create_index("plan_exemplars_fts_idx", "plan_exemplars", ["fts"], postgresql_using="gin")

    op.create_table(
        "routing_stats",
        sa.Column("capability_key", sa.String(320), primary_key=True),
        sa.Column("rung", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=True),
        sa.Column("entity_name", sa.String(255), nullable=True),
        sa.Column("runs_total", sa.Integer(), nullable=False),
        sa.Column("runs_completed", sa.Integer(), nullable=False),
        sa.Column("runs_failed", sa.Integer(), nullable=False),
        sa.Column("hitl_denied", sa.Integer(), nullable=False),
        sa.Column("mean_input_tokens", sa.Float(), nullable=False),
        sa.Column("mean_output_tokens", sa.Float(), nullable=False),
        sa.Column("mean_duration_ms", sa.Float(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    for table in (
        "routing_stats",
        "plan_exemplars",
        "conversation_rollups",
        "run_digests",
        "memory_entity_links",
        "memory_entities",
        "memory_embeddings",
        "memories",
    ):
        op.drop_table(table)
