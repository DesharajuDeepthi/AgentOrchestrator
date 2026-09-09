"""skills registry + skill_tools join (spec §3.3)."""

from typing import Any

from sqlalchemy import Boolean, Column, ForeignKey, Table, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, RegistryRecord
from app.models.tool import Tool

skill_tools = Table(
    "skill_tools",
    Base.metadata,
    Column("skill_id", ForeignKey("skills.id"), primary_key=True),
    Column("tool_id", ForeignKey("tools.id"), primary_key=True),
)


class Skill(RegistryRecord):
    __tablename__ = "skills"

    kind: Mapped[str] = mapped_column(default="custom")  # 'native' | 'custom'
    persona: Mapped[str] = mapped_column(Text, default="")
    instructions: Mapped[str] = mapped_column(Text, default="")
    direct_exposure: Mapped[bool] = mapped_column(Boolean, default=False)
    # optional per-skill model override (spec §3.3): null inherits from the
    # invoking sub agent, then settings defaults
    model: Mapped[str | None] = mapped_column(Text, default=None)
    model_params: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    # per-skill loop budget override (spec §3.3): null inherits settings
    max_tool_iterations: Mapped[int | None] = mapped_column(default=None)
    # retrieval vector (spec §7.4): maintained best-effort on the write path
    embedding: Mapped[list[Any] | None] = mapped_column(default=None)
    embedding_hash: Mapped[str | None] = mapped_column(Text, default=None)

    tools: Mapped[list[Tool]] = relationship(secondary=skill_tools, lazy="selectin")
