"""Shared test fixtures: test database, app client, fake LLM scripting."""

import os

os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5433/concierge_test",
)
os.environ["FAKE_LLM_ENABLED"] = "1"
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("GOOGLE_API_KEY", None)
os.environ.pop("OPENAI_API_KEY", None)

from collections.abc import AsyncIterator, Iterator  # noqa: E402

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.config import get_config  # noqa: E402
from app.db import get_engine, get_session_factory, reset_db_state  # noqa: E402
from app.llm import fake as fake_llm  # noqa: E402
from app.llm.registry import register_builtin_providers  # noqa: E402
from app.memory import scheduler as memory_scheduler  # noqa: E402
from app.models import Base  # noqa: E402

get_config.cache_clear()
register_builtin_providers()


@pytest.fixture(scope="session", autouse=True)
async def _database() -> AsyncIterator[None]:
    reset_db_state()
    engine = get_engine()
    async with engine.begin() as conn:
        # memory tables use pgvector (spec §16.1); the test image ships it
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


# The fire-and-forget §16.2 post-run pipeline debounces 1s in prod; under
# suite load a task spawned by test N can land DURING test N+1, racing its
# fake-LLM script queue and the truncate below (the test_memory_semantic
# order-flake shape). Tests shrink the debounce and drain at every boundary.
memory_scheduler._POST_RUN_DEBOUNCE_S = 0.01


@pytest.fixture(autouse=True)
async def _clean_tables(_database: None) -> AsyncIterator[None]:
    yield
    # no §16.2 background task may cross a test boundary — settle them
    # BEFORE the truncate so none replays against the next test's world
    await memory_scheduler.drain()
    engine = get_engine()
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} CASCADE"))


@pytest.fixture(autouse=True)
def _clean_fake_script() -> Iterator[None]:
    fake_llm.clear_script()
    yield
    fake_llm.clear_script()


@pytest.fixture(autouse=True)
def _reset_registry_cache() -> Iterator[None]:
    """Fresh cache singleton per test — mode + memory state never leak."""
    from app.registry_cache import reset_cache

    reset_cache()
    yield
    reset_cache()


@pytest.fixture(autouse=True)
def _reset_sse_shutdown_flag() -> Iterator[None]:
    """sse-starlette monkey-patches uvicorn.Server.handle_exit, so ANY
    in-process uvicorn shutdown (the MCP http-transport tests run one) sets
    AppStatus.should_exit process-wide and permanently — after which every
    later EventSourceResponse drains instantly and SSE tests in OTHER files
    return empty streams. Reset the leaked global after every test."""
    yield
    from sse_starlette.sse import AppStatus

    AppStatus.should_exit = False


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_session_factory()() as s:
        yield s


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from app.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def seeded_client(client: AsyncClient) -> AsyncClient:
    resp = await client.post("/api/v1/seed/reload")
    assert resp.status_code == 200, resp.text
    return client
