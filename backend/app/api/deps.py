"""Shared router helpers: list filters, static-record write rules (spec §4)."""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import RegistryRecord

SessionDep = Annotated[AsyncSession, Depends(get_session)]


class ListFilters(BaseModel):
    include_deleted: bool = False
    source: str | None = None
    q: str | None = None


def list_filters(
    include_deleted: bool = Query(default=False),
    source: str | None = Query(default=None),
    q: str | None = Query(default=None),
) -> ListFilters:
    return ListFilters(include_deleted=include_deleted, source=source, q=q)


FiltersDep = Annotated[ListFilters, Depends(list_filters)]


def apply_filters[R: RegistryRecord](
    stmt: Select[tuple[R]], model: type[R], f: ListFilters
) -> Select[tuple[R]]:
    if not f.include_deleted:
        stmt = stmt.where(model.deleted_at.is_(None))
    if f.source:
        stmt = stmt.where(model.source == f.source)
    if f.q:
        pattern = f"%{f.q}%"
        stmt = stmt.where(model.name.ilike(pattern) | model.description.ilike(pattern))
    return stmt.order_by(model.created_at)


async def fetch_or_404[R: RegistryRecord](
    session: AsyncSession, model: type[R], record_id: UUID
) -> R:
    record = await session.get(model, record_id)
    if record is None or record.deleted_at is not None:
        raise HTTPException(status_code=404, detail=f"{model.__name__} {record_id} not found")
    return record


# The single static-record exception (spec §4): status — plus direct_exposure
# where applicable — stays togglable so the command center can switch anything
# off without editing its definition.
STATIC_TOGGLABLE = {"status", "direct_exposure"}


def enforce_static_rules(record: RegistryRecord, changed_fields: set[str]) -> None:
    if record.source != "static":
        return
    locked = changed_fields - STATIC_TOGGLABLE
    if locked:
        raise HTTPException(
            status_code=403,
            detail=(
                f"record is static; only {sorted(STATIC_TOGGLABLE)} are togglable — "
                f"rejected fields: {sorted(locked)}"
            ),
        )


def reject_static_delete(record: RegistryRecord) -> None:
    if record.source == "static":
        raise HTTPException(
            status_code=403,
            detail="static records cannot be deleted; toggle status to 'inactive' instead",
        )
