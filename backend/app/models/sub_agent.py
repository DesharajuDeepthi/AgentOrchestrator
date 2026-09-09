"""sub_agents registry + sub_agent_skills join (spec §3.4)."""

from typing import Any

from sqlalchemy import Boolean, Column, ForeignKey, String, Table, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, RegistryRecord
from app.models.skill import Skill

sub_agent_skills = Table(
    "sub_agent_skills",
    Base.metadata,
    Column("sub_agent_id", ForeignKey("sub_agents.id"), primary_key=True),
    Column("skill_id", ForeignKey("skills.id"), primary_key=True),
)


class SubAgent(RegistryRecord):
    __tablename__ = "sub_agents"

    kind: Mapped[str] = mapped_column(String(16), default="custom")  # 'native' | 'custom'
    persona: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str | None] = mapped_column(String(255), default=None)
    model_params: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    workflow: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    native_ref: Mapped[str | None] = mapped_column(String(512), default=None)
    # direct invocation gate (spec §7.5): togglable on static records like
    # the tools/skills flag it mirrors
    direct_exposure: Mapped[bool] = mapped_column(Boolean, default=False)
    covers_skill_ids: Mapped[list[Any] | None] = mapped_column(default=None)
    # retrieval vector (spec §7.4): maintained best-effort on the write path
    embedding: Mapped[list[Any] | None] = mapped_column(default=None)
    embedding_hash: Mapped[str | None] = mapped_column(String(64), default=None)

    skills: Mapped[list[Skill]] = relationship(secondary=sub_agent_skills, lazy="selectin")
