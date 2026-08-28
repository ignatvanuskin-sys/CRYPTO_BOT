from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

# PostgreSQL advisory locks are connection-scoped; keep the connection open.
WORKER_LOCK_KEY = 82463517
BOT_LOCK_KEY = 82463518


async def acquire_advisory_lock(engine: AsyncEngine, lock_key: int, owner: str) -> AsyncConnection | None:
    if engine.dialect.name != "postgresql":
        return None
    connection = await engine.connect()
    acquired = await connection.scalar(
        text("SELECT pg_try_advisory_lock(:lock_key)"),
        {"lock_key": lock_key},
    )
    if not acquired:
        await connection.close()
        raise RuntimeError(f"Another {owner} process already holds the singleton lock")
    return connection


async def release_advisory_lock(connection: AsyncConnection | None, lock_key: int) -> None:
    if connection is None:
        return
    try:
        await connection.execute(
            text("SELECT pg_advisory_unlock(:lock_key)"),
            {"lock_key": lock_key},
        )
    finally:
        await connection.close()


async def acquire_worker_lock(engine: AsyncEngine) -> AsyncConnection | None:
    return await acquire_advisory_lock(engine, WORKER_LOCK_KEY, "worker")


async def release_worker_lock(connection: AsyncConnection | None) -> None:
    await release_advisory_lock(connection, WORKER_LOCK_KEY)
