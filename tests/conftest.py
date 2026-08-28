import os
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from db.base import Base
import db.paper_models  # ensure paper tables are registered for create_all
import db.competition_models  # competitions, participants, executions
import db.market_data  # shared authoritative market snapshots

# ---- engine selection ----
# sqlite (aiosqlite) is valid ONLY for non-concurrent logic.
# Postgres is required for: FOR UPDATE, race, unique-key races.

@pytest.fixture(autouse=True)
def clear_local_market_cache():
    from services.bingx_market_data import _price_cache as _bingx_cache
    _bingx_cache.clear()
    yield
    _bingx_cache.clear()

@pytest_asyncio.fixture
async def sqlite_engine():
    """aiosqlite in-memory — допустим только для тестов без блокировок."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()

@pytest_asyncio.fixture
async def session(sqlite_engine):
    """Default session on sqlite — для простых CRUD/idempotency тестов."""
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as s:
        yield s

# ---- Postgres fixture (real) ----
# Tries: 1) TEST_DATABASE_URL, 2) DATABASE_URL, 3) testcontainers, 4) skip.
@pytest_asyncio.fixture
async def pg_engine():
    """
    Реальный Postgres для денежных тестов с блокировками.
    Engine = asyncpg. Берёт URL из окружения (TEST_DATABASE_URL или DATABASE_URL).
    """
    url = os.getenv("TEST_DATABASE_URL", "") or os.getenv("DATABASE_URL", "")
    if url and "postgres" in url:
        eng = create_async_engine(url, echo=False)
        try:
            async with eng.begin() as conn:
                # clean + create for test isolation
                await conn.run_sync(Base.metadata.drop_all)
                await conn.run_sync(Base.metadata.create_all)
            yield eng
        finally:
            async with eng.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
            await eng.dispose()
        return

    try:
        from testcontainers.postgres import PostgresContainer
        import asyncpg  # noqa: F401
    except Exception as e:
        pytest.skip(f"testcontainers/asyncpg not available: {e}")

    try:
        container = PostgresContainer("postgres:15-alpine")
        container.start()
    except Exception as e:
        pytest.skip(f"Docker unavailable for PostgresContainer: {e}")

    try:
        raw_url = container.get_connection_url()
        async_url = raw_url.replace("psycopg2://", "postgresql+asyncpg://").replace("postgresql://", "postgresql+asyncpg://")
        eng = create_async_engine(async_url, echo=False)
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield eng
        await eng.dispose()
    finally:
        try:
            container.stop()
        except Exception:
            pass

@pytest_asyncio.fixture
async def pg_session(pg_engine):
    factory = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with factory() as s:
        yield s
