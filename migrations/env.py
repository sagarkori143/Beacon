"""Alembic environment.

Migrations run through the same async driver as the application, so there is no
second PostgreSQL driver to install or keep in sync.

Migrations connect as the **owner** role. The application connects as a separate
non-owner, non-BYPASSRLS role, which is what makes Row Level Security actually
bind: a table's owner is exempt from its own policies unless ``FORCE ROW LEVEL
SECURITY`` is set, and relying on that alone would mean one missed FORCE is a
silent tenant leak.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

settings = get_settings()
DATABASE_URL = config.get_main_option("sqlalchemy.url") or settings.database_url


def include_object(obj, name, type_, reflected, compare_to) -> bool:  # type: ignore[no-untyped-def]
    """Keep autogenerate from touching things it does not own.

    The vector and tsvector indexes are created by hand with options Alembic
    cannot express; without this it proposes dropping them on every run.
    """
    if type_ == "index" and name in {
        "ix_chunks_embedding_hnsw",
        "ix_chunks_search_vector",
    }:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
