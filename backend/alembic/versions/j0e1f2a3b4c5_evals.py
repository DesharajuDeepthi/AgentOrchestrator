"""evals (spec §15 — milestone M32)

Revision ID: j0e1f2a3b4c5
Revises: i9d0e1f2a3b4
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "j0e1f2a3b4c5"
down_revision: str | None = "i9d0e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "eval_datasets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("level", sa.String(16), nullable=False),
        sa.Column("target_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_table(
        "eval_cases",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dataset_id",
            UUID(as_uuid=True),
            sa.ForeignKey("eval_datasets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("input", sa.Text, nullable=False),
        sa.Column("expected", sa.Text, nullable=False, server_default=""),
        sa.Column("judge_notes", sa.Text, nullable=False, server_default=""),
        sa.Column("grader", sa.String(16), nullable=False, server_default="llm_judge"),
        sa.Column("position", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_table(
        "eval_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dataset_id",
            UUID(as_uuid=True),
            sa.ForeignKey("eval_datasets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("config_snapshot", JSONB, nullable=True),
        sa.Column("total_cases", sa.Integer, nullable=False, server_default="0"),
        sa.Column("passed_cases", sa.Integer, nullable=False, server_default="0"),
        sa.Column("failed_cases", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error_cases", sa.Integer, nullable=False, server_default="0"),
        sa.Column("langsmith_url", sa.String(512), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "eval_results",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "eval_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("eval_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "case_id",
            UUID(as_uuid=True),
            sa.ForeignKey("eval_cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="graded"),
        sa.Column("passed", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("score", sa.Float, nullable=False, server_default="0"),
        sa.Column("grader_reason", sa.Text, nullable=False, server_default=""),
        sa.Column("answer", sa.Text, nullable=False, server_default=""),
    )
    # §15 eval provenance on ordinary runs
    op.add_column("runs", sa.Column("is_eval", sa.Boolean, nullable=False, server_default="false"))
    op.add_column("runs", sa.Column("eval_skill_id", UUID(as_uuid=True), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "eval_skill_id")
    op.drop_column("runs", "is_eval")
    op.drop_table("eval_results")
    op.drop_table("eval_runs")
    op.drop_table("eval_cases")
    op.drop_table("eval_datasets")
