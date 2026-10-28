"""Alembic asyncpg environment; lock and transaction cover the whole revision chain."""
from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy import text

from schema_migrations import LOCK_ID, migration_engine, record_compatibility

config = context.config


def run(connection) -> None:
    def applied(**_event) -> None:
        record_compatibility(connection)

    context.configure(connection=connection, target_metadata=None, compare_type=True,
                      on_version_apply=applied)
    with context.begin_transaction():
        context.run_migrations()


async def online() -> None:
    # Programmatic callers can share the explicit migration transaction with validation.
    supplied = config.attributes.get("connection")
    if supplied is not None:
        run(supplied)
        return
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        raise RuntimeError("DATABASE_URL must select the migration owner connection")
    engine = migration_engine(dsn)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("SET LOCAL lock_timeout = '30s'"))
            await connection.execute(text("SET LOCAL statement_timeout = '5min'"))
            await connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK_ID})
            await connection.run_sync(run)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(url="postgresql://", target_metadata=None, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    supplied = config.attributes.get("connection")
    if supplied is not None:
        run(supplied)
    else:
        asyncio.run(online())
