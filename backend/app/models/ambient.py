"""Ambient mode substrate tables (spec §17.1 — milestone M20).

Invariants enforced here and in the stores, not in prompts:
- ambient_events is append-only; payloads are UNTRUSTED input end-to-end
- routine fire tokens are stored hashed, shown once at issue time
- static routine definitions are immutable (§4 discipline: status toggles only)
- derived events carry causation/correlation/depth for the §17.3a guards
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

EVENT_SOURCES = {
    "schedule",
    "webhook",
    "poll",
    "internal",
    "wakeup",
    "presence",
    "pattern",
    "manual",
}
EVENT_VERDICTS = {"fired", "held", "expired", "dropped"}
ROUTINE_AUTONOMY = {"propose", "act_reversible"}
INTENT_CONDITIONS = {"event", "state", "time"}
DELIVERY_TIERS = {0, 1, 2, 3}  # interrupt | notify | digest | silent
PRESENCE_STATES = {"active", "idle", "away", "offline"}


class AmbientEvent(Base):
    """One observed occurrence entering the trigger plane (spec §17.2)."""

    __tablename__ = "ambient_events"
    __table_args__ = (
        Index("ambient_events_pending_idx", "received_at", postgresql_where="verdict IS NULL"),
        Index("ambient_events_dedupe_idx", "dedupe_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict[str, Any] | None] = mapped_column(default=None)  # UNTRUSTED
    dedupe_key: Mapped[str | None] = mapped_column(String(255), default=None)
    routine_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("routines.id", ondelete="SET NULL"), default=None
    )
    intent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("standing_intents.id", ondelete="SET NULL"), default=None
    )
    # §17.3a chaining provenance
    causation_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    depth: Mapped[int] = mapped_column(SmallInteger, default=0)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    verdict: Mapped[str | None] = mapped_column(String(16), default=None)  # NULL = pending
    verdict_reason: Mapped[str | None] = mapped_column(Text, default=None)
    # §17.3 fire/hold ledger detail: {value, urgency, attention_state, tier}
    decision: Mapped[dict[str, Any] | None] = mapped_column(default=None)


class Routine(Base):
    """A stored, trusted ambient work definition (spec §17.4)."""

    __tablename__ = "routines"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # §18.8 tenancy: owner when auth is on; NULL in the single-user regime
    user_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    prompt: Mapped[str] = mapped_column(Text)  # TRUSTED — stored by an authorized session
    source: Mapped[str] = mapped_column(String(16), default="dynamic")  # 'static' | 'dynamic'
    triggers: Mapped[list[Any] | None] = mapped_column(default=None)
    # narrowed registry projection: refs like {'skills': [...], 'tools': [...]}
    allowlist: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    model_ref: Mapped[str | None] = mapped_column(String(255), default=None)
    # §18.2 cross-fire continuity: fires reuse ONE persistent conversation
    include_memories: Mapped[bool] = mapped_column(default=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    autonomy: Mapped[str] = mapped_column(String(16), default="propose")
    budgets: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    fire_token_hash: Mapped[str | None] = mapped_column(String(128), default=None)
    stagger_offset_s: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|paused|error
    status_reason: Mapped[str | None] = mapped_column(Text, default=None)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class StandingIntent(Base):
    """A typed, durable "tell me when…" row (spec §17.4) — never a remembered
    prompt: the scheduler owns *when to evaluate*, the LLM only evaluates
    semantic predicates and composes messages."""

    __tablename__ = "standing_intents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # §18.8 tenancy: owner when auth is on; NULL in the single-user regime
    user_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    text: Mapped[str] = mapped_column(Text)  # the user's words
    condition_type: Mapped[str] = mapped_column(String(8))  # event|state|time
    compiled: Mapped[dict[str, Any] | None] = mapped_column(default=None)  # typed rule
    semantic_predicate: Mapped[str | None] = mapped_column(Text, default=None)
    judge_model_ref: Mapped[str | None] = mapped_column(String(255), default=None)
    window: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    watermark: Mapped[str | None] = mapped_column(String(255), default=None)
    cadence_s: Mapped[int] = mapped_column(Integer, default=300)
    # adaptive polling state (spec §17.2)
    base_interval_s: Mapped[int] = mapped_column(Integer, default=300)
    current_interval_s: Mapped[int] = mapped_column(Integer, default=300)
    max_interval_s: Mapped[int] = mapped_column(Integer, default=3600)
    backoff_multiplier: Mapped[float] = mapped_column(Float, default=1.5)
    consecutive_quiet: Mapped[int] = mapped_column(Integer, default=0)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    budget: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    delivery: Mapped[str] = mapped_column(String(16), default="auto")  # digest|interrupt|auto
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AmbientWakeup(Base):
    """Agent-scheduled self-wakeup (spec §17.4, heartbeat sense H2)."""

    __tablename__ = "ambient_wakeups"
    __table_args__ = (
        Index("ambient_wakeups_due_idx", "due_at", postgresql_where="status = 'pending'"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    routine_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("routines.id", ondelete="CASCADE"), default=None
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), default=None
    )
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    created_by: Mapped[str] = mapped_column(String(8), default="agent")  # agent|system|user
    status: Mapped[str] = mapped_column(
        String(16), default="pending"
    )  # pending|fired|cancelled|expired
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class PatternInstance(Base):
    """A partial composite-pattern match (spec §17.3a). Absence is an armed
    timer: `deadline_at` fires on the tick, never a continuous query."""

    __tablename__ = "pattern_instances"
    __table_args__ = (
        Index(
            "pattern_instances_armed_uq",
            "rule_key",
            "partition_key",
            unique=True,
            postgresql_where="state = 'armed'",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    rule_key: Mapped[str] = mapped_column(String(255))  # routine/intent-scoped rule id
    partition_key: Mapped[str] = mapped_column(String(255), default="")
    state: Mapped[str] = mapped_column(String(8), default="armed")  # armed|matched|expired
    a_event_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Delivery(Base):
    """Outbox row for anything ambient wants a human to see (spec §17.5)."""

    __tablename__ = "deliveries"
    __table_args__ = (
        Index(
            "deliveries_pending_idx", "tier", "created_at", postgresql_where="delivered_at IS NULL"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # §18.8 tenancy: owner when auth is on; NULL in the single-user regime
    user_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), default=None
    )
    intent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("standing_intents.id", ondelete="SET NULL"), default=None
    )
    category: Mapped[str] = mapped_column(String(64), default="general")
    tier: Mapped[int] = mapped_column(SmallInteger, default=2)  # 0..3
    urgency: Mapped[int] = mapped_column(SmallInteger, default=2)  # 1..5
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str | None] = mapped_column(Text, default=None)
    deliver_no_later_than: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    channel: Mapped[str | None] = mapped_column(String(32), default=None)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(default=None)
    # supersede-collapse key (spec §17.5): pending items sharing it are
    # superseded by the newest arrival
    skey: Mapped[str | None] = mapped_column(String(255), default=None)
    # §18.4 per-channel send ledger: {channel: {ok, error, at}}
    external: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    # M42: when a human actually opened it (§18.4) — null means unseen, which
    # is what the §17.5 salience pass keys on
    seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # M42: the §17.5 salience verdict {verdict, reason, confidence, at, mode}
    salience: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    feedback: Mapped[str | None] = mapped_column(String(16), default=None)
    reward: Mapped[float | None] = mapped_column(Float, default=None)  # §17.7 substrate
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserPresence(Base):
    """Single-user presence snapshot (spec §17.5) — POC is single-tenant, so
    one row keyed by a fixed id; the schema leaves room for more."""

    __tablename__ = "user_presence"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default="default")
    # §18.8: per-user rows keyed "user:{uuid}" carry the owner; "default"
    # stays the single-user row (user_id NULL)
    user_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    state: Mapped[str] = mapped_column(String(8), default="offline")
    visible: Mapped[bool] = mapped_column(default=False)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    # when the current state began — user_returned carries away_s (§17.5)
    state_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AmbientPolicy(Base):
    """Append-only category policy ledger (spec §17.6/§17.7): the latest row
    per category wins; history is the audit trail and the revert path."""

    __tablename__ = "ambient_policies"
    __table_args__ = (Index("ambient_policies_cat_idx", "category", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # §18.8 tenancy: owner when auth is on; NULL in the single-user regime
    user_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    category: Mapped[str] = mapped_column(String(64))
    tier_override: Mapped[int | None] = mapped_column(SmallInteger, default=None)
    reason: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16), default="rule")  # rule|learner|user
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
