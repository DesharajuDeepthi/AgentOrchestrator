"""Alembic environment — sync engine over the app's DATABASE_URL."""

from sqlalchemy import create_engine, pool

from alembic import context
from app.config import get_config
from app.models import Base

config = context.config
target_metadata = Base.metadata


def _sync_url() -> str:
    # alembic runs sync; swap the asyncpg driver for psycopg
    return get_config().database_url.replace("+asyncpg", "+psycopg")


def run_migrations_offline() -> None:
    context.configure(
        url=_sync_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_sync_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
