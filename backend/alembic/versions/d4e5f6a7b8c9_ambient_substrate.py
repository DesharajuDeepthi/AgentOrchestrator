"""Ambient substrate (spec §17.1): seven tables + runs trigger/liveness columns.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "routines",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("description", sa.Text),
        sa.Column("prompt", sa.Text, nullable=False),
        sa.Column("source", sa.String(16), nullable=False, server_default="dynamic"),
        sa.Column("triggers", JSONB),
        sa.Column("allowlist", JSONB),
        sa.Column("model_ref", sa.String(255)),
        sa.Column("autonomy", sa.String(16), nullable=False, server_default="propose"),
        sa.Column("budgets", JSONB),
        sa.Column("fire_token_hash", sa.String(128)),
        sa.Column("stagger_offset_s", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("status_reason", sa.Text),
        sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_fired_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "standing_intents",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("condition_type", sa.String(8), nullable=False),
        sa.Column("compiled", JSONB),
        sa.Column("semantic_predicate", sa.Text),
        sa.Column("judge_model_ref", sa.String(255)),
        sa.Column("window", JSONB),
        sa.Column("watermark", sa.String(255)),
        sa.Column("cadence_s", sa.Integer, nullable=False, server_default="300"),
        sa.Column("base_interval_s", sa.Integer, nullable=False, server_default="300"),
        sa.Column("current_interval_s", sa.Integer, nullable=False, server_default="300"),
        sa.Column("max_interval_s", sa.Integer, nullable=False, server_default="3600"),
        sa.Column("backoff_multiplier", sa.Float, nullable=False, server_default="1.5"),
        sa.Column("consecutive_quiet", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("budget", JSONB),
        sa.Column("delivery", sa.String(16), nullable=False, server_default="auto"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "ambient_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("payload", JSONB),
        sa.Column("dedupe_key", sa.String(255)),
        sa.Column(
            "routine_id", UUID(as_uuid=True), sa.ForeignKey("routines.id", ondelete="SET NULL")
        ),
        sa.Column(
            "intent_id",
            UUID(as_uuid=True),
            sa.ForeignKey("standing_intents.id", ondelete="SET NULL"),
        ),
        sa.Column("causation_id", UUID(as_uuid=True)),
        sa.Column("correlation_id", UUID(as_uuid=True)),
        sa.Column("depth", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("verdict", sa.String(16)),
        sa.Column("verdict_reason", sa.Text),
    )
    op.create_index(
        "ambient_events_pending_idx",
        "ambient_events",
        ["received_at"],
        postgresql_where=sa.text("verdict IS NULL"),
    )
    op.create_index("ambient_events_dedupe_idx", "ambient_events", ["dedupe_key"])
    op.create_table(
        "ambient_wakeups",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "routine_id", UUID(as_uuid=True), sa.ForeignKey("routines.id", ondelete="CASCADE")
        ),
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("runs.id", ondelete="SET NULL")),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("payload", JSONB),
        sa.Column("created_by", sa.String(8), nullable=False, server_default="agent"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("fired_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ambient_wakeups_due_idx",
        "ambient_wakeups",
        ["due_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "pattern_instances",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("rule_key", sa.String(255), nullable=False),
        sa.Column("partition_key", sa.String(255), nullable=False, server_default=""),
        sa.Column("state", sa.String(8), nullable=False, server_default="armed"),
        sa.Column("a_event_id", UUID(as_uuid=True)),
        sa.Column("deadline_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "pattern_instances_armed_uq",
        "pattern_instances",
        ["rule_key", "partition_key"],
        unique=True,
        postgresql_where=sa.text("state = 'armed'"),
    )
    op.create_table(
        "deliveries",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("runs.id", ondelete="SET NULL")),
        sa.Column(
            "intent_id",
            UUID(as_uuid=True),
            sa.ForeignKey("standing_intents.id", ondelete="SET NULL"),
        ),
        sa.Column("category", sa.String(64), nullable=False, server_default="general"),
        sa.Column("tier", sa.SmallInteger, nullable=False, server_default="2"),
        sa.Column("urgency", sa.SmallInteger, nullable=False, server_default="2"),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body", sa.Text),
        sa.Column("deliver_no_later_than", sa.DateTime(timezone=True)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("channel", sa.String(32)),
        sa.Column("superseded_by", UUID(as_uuid=True)),
        sa.Column("feedback", sa.String(16)),
        sa.Column("reward", sa.Float),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "deliveries_pending_idx",
        "deliveries",
        ["tier", "created_at"],
        postgresql_where=sa.text("delivered_at IS NULL"),
    )
    op.create_table(
        "user_presence",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("state", sa.String(8), nullable=False, server_default="offline"),
        sa.Column("visible", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("last_activity_at", sa.DateTime(timezone=True)),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.add_column("runs", sa.Column("trigger", JSONB))
    op.add_column("runs", sa.Column("last_heartbeat_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("runs", "last_heartbeat_at")
    op.drop_column("runs", "trigger")
    op.drop_table("user_presence")
    op.drop_table("deliveries")
    op.drop_table("pattern_instances")
    op.drop_table("ambient_wakeups")
    op.drop_table("ambient_events")
    op.drop_table("standing_intents")
    op.drop_table("routines")
