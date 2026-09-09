"""Async SQLAlchemy engine and session factory."""

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_config

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_config().database_url, pool_pre_ping=True)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding one session per request."""
    async with get_session_factory()() as session:
        yield session


_checkpointer: Any = None
_checkpointer_pool: Any = None


async def get_checkpointer() -> Any:
    """Shared LangGraph Postgres checkpointer (spec §6) — required for HITL
    pause/resume; threads are keyed by run_id."""
    global _checkpointer, _checkpointer_pool
    if _checkpointer is None:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        url = get_config().database_url.replace("+asyncpg", "")
        _checkpointer_pool = AsyncConnectionPool(
            url,
            open=False,
            max_size=10,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        await _checkpointer_pool.open()
        _checkpointer = AsyncPostgresSaver(_checkpointer_pool)
        await _checkpointer.setup()
    return _checkpointer


async def close_checkpointer() -> None:
    global _checkpointer, _checkpointer_pool
    if _checkpointer_pool is not None:
        await _checkpointer_pool.close()
    _checkpointer = None
    _checkpointer_pool = None


def reset_db_state() -> None:
    """Testing hook: forget cached engine/factory so a new DATABASE_URL applies."""
    global _engine, _session_factory, _checkpointer, _checkpointer_pool
    _engine = None
    _session_factory = None
    _checkpointer = None
    _checkpointer_pool = None
