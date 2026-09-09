"""Memory API schemas (spec §16.6) — Pydantic v2, separate from ORM."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MemoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    scope: str
    conversation_id: UUID | None
    kind: str
    text: str
    payload: dict[str, Any] | None
    entity_key: str | None
    importance: int
    confidence: float
    source: str
    status: str
    valid_from: datetime
    valid_to: datetime | None
    recorded_at: datetime
    superseded_at: datetime | None
    supersedes: UUID | None
    superseded_by: UUID | None
    run_id: UUID | None
    step_id: UUID | None
    last_accessed_at: datetime
    access_count: int
    pinned: bool
    half_life_days: float | None
    review_note: str | None


class MemoryCreate(BaseModel):
    """UI/API create — always `user_stated` (the human is the author)."""

    text: str = Field(min_length=1, max_length=2000)
    kind: str = "fact"
    scope: str = "global"
    conversation_id: UUID | None = None
    payload: dict[str, Any] | None = None
    entity_key: str | None = None
    importance: int = Field(default=5, ge=1, le=10)
    pinned: bool = False


class MemoryPatch(BaseModel):
    """UI actions: pin/unpin, edit-as-supersede (text), review decisions."""

    pinned: bool | None = None
    text: str | None = Field(default=None, min_length=1, max_length=2000)
    importance: int | None = Field(default=None, ge=1, le=10)
    review: str | None = Field(default=None, pattern="^(approve|reject)$")
    review_note: str | None = None


class RecallOut(BaseModel):
    memory: MemoryOut
    score: float
    relevance: float
    recency: float
    importance: float


class MemoryStatusOut(BaseModel):
    counts: dict[str, int]
    by_kind: dict[str, int]
    quarantined: int
    pinned: int
    embeddings: int
