import os
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from db.base import Base
import db.paper_models  # ensure new tables are registered for create_all
import db.competition_models  # competitions, participants, executions
import db.market_data  # shared authoritative market snapshots
from datetime import datetime, timezone
from decimal import Decimal

# ---- engine selection ----
# All tests explicitly declare which engine they use in docstring.
# sqlite (aiosqlite) is valid ONLY for non-concurrent logic.
# Postgres is required for: FOR UPDATE, race, CHECK constraints race, weekly atomicity.

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
# Tries: 1) TEST_DATABASE_URL, 2) DATABASE_URL (тот же что для ручного тестирования бота), 3) testcontainers, 4) skip.
@pytest_asyncio.fixture
async def pg_engine():
    """
    Реальный Postgres для денежных тестов с блокировками.
    Engine = asyncpg. Берёт URL из окружения (TEST_DATABASE_URL или DATABASE_URL),
    чтобы использовать ту же БД что и для ручного тестирования бота.
    Если нет — пробует testcontainers. Пропускается только если Docker недоступен.
    """
    url = os.getenv("TEST_DATABASE_URL", "") or os.getenv("DATABASE_URL", "")
    # 1) explicit URL (env) — использует ту же БД что и ручное тестирование бота
    if url and "postgres" in url:
        eng = create_async_engine(url, echo=False)
        try:
            async with eng.begin() as conn:
                # clean + create for test isolation
                await conn.run_sync(Base.metadata.drop_all)
                await conn.run_sync(Base.metadata.create_all)
                # partial unique index for WEEKLY_GRANT — alembic only, Base.metadata его не создаёт
                from sqlalchemy import text
                await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_weekly_grant ON transactions (user_id, week_id) WHERE type='WEEKLY_GRANT'"))
            yield eng
        finally:
            # cleanup after test function
            async with eng.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
            await eng.dispose()
        return

    # 2) testcontainers
    try:
        from testcontainers.postgres import PostgresContainer
        import asyncpg  # noqa: F401
    except Exception as e:
        pytest.skip(f"testcontainers/asyncpg not available: {e}")

    # check docker available
    try:
        container = PostgresContainer("postgres:15-alpine")
        container.start()
    except Exception as e:
        pytest.skip(f"Docker unavailable for PostgresContainer: {e}")

    try:
        raw_url = container.get_connection_url()
        # sqlalchemy asyncpg url
        async_url = raw_url.replace("psycopg2://", "postgresql+asyncpg://").replace("postgresql://", "postgresql+asyncpg://")
        eng = create_async_engine(async_url, echo=False)
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            from sqlalchemy import text
            await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_weekly_grant ON transactions (user_id, week_id) WHERE type='WEEKLY_GRANT'"))
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

@pytest_asyncio.fixture
async def make_user_week(session):
    """Helper on sqlite session (for non-PG tests)."""
    from db.models import User, Week, Asset, Transaction
    from sqlalchemy import select
    async def _make(telegram_id=1000, username="test", phone_verified=True, is_banned=False, grant=Decimal("10000"), eligible=True):
        user = User(telegram_id=telegram_id, username=username, is_banned=is_banned)
        if phone_verified:
            user.phone_number = f"+7900{telegram_id}"
            user.phone_verified_at = datetime.now(timezone.utc)
            user.rules_accepted_at = datetime.now(timezone.utc)
        session.add(user)
        await session.flush()
        result = await session.execute(select(Week))
        week = result.scalars().first()
        if not week:
            week = Week(week_number=1, starts_at=datetime.now(timezone.utc), ends_at=datetime.now(timezone.utc), status="active")
            session.add(week)
            await session.flush()
        for sym in ["BTC-USDT", "ETH-USDT", "SHIT-USDT"]:
            res = await session.execute(select(Asset).where(Asset.symbol == sym))
            if res.scalar_one_or_none() is None:
                is_elig = eligible if sym != "SHIT-USDT" else False
                asset = Asset(symbol=sym, base_asset=sym.split("-")[0], quote_asset="USDT", status="active", is_quote_eligible=is_elig, last_24h_quote_volume=Decimal("2000000"), updated_at=datetime.now(timezone.utc))
                session.add(asset)
        await session.flush()
        if grant is not None:
            tx = Transaction(user_id=user.id, week_id=week.id, type="WEEKLY_GRANT", amount=grant, balance_after=grant, idempotency_key=f"grant:{week.id}:{user.id}")
            session.add(tx)
            await session.flush()
        await session.commit()
        result = await session.execute(select(Week).where(Week.id == week.id))
        week = result.scalar_one()
        return user, week
    return _make

@pytest_asyncio.fixture
async def make_pg_user_week(pg_session):
    """Helper on pg_session for PG tests."""
    from db.models import User, Week, Asset, Transaction
    from sqlalchemy import select
    async def _make(telegram_id=1000, username="test", phone_verified=True, is_banned=False, grant=Decimal("10000"), eligible=True):
        user = User(telegram_id=telegram_id, username=username, is_banned=is_banned)
        if phone_verified:
            user.phone_number = f"+7900{telegram_id}"
            user.phone_verified_at = datetime.now(timezone.utc)
            user.rules_accepted_at = datetime.now(timezone.utc)
        pg_session.add(user)
        await pg_session.flush()
        result = await pg_session.execute(select(Week))
        week = result.scalars().first()
        if not week:
            week = Week(week_number=1, starts_at=datetime.now(timezone.utc), ends_at=datetime.now(timezone.utc), status="active")
            pg_session.add(week)
            await pg_session.flush()
        for sym in ["BTC-USDT", "ETH-USDT", "SHIT-USDT"]:
            res = await pg_session.execute(select(Asset).where(Asset.symbol == sym))
            if res.scalar_one_or_none() is None:
                is_elig = eligible if sym != "SHIT-USDT" else False
                asset = Asset(symbol=sym, base_asset=sym.split("-")[0], quote_asset="USDT", status="active", is_quote_eligible=is_elig, last_24h_quote_volume=Decimal("2000000"), updated_at=datetime.now(timezone.utc))
                pg_session.add(asset)
        await pg_session.flush()
        if grant is not None:
            tx = Transaction(user_id=user.id, week_id=week.id, type="WEEKLY_GRANT", amount=grant, balance_after=grant, idempotency_key=f"grant:{week.id}:{user.id}")
            pg_session.add(tx)
            await pg_session.flush()
        await pg_session.commit()
        result = await pg_session.execute(select(Week).where(Week.id == week.id))
        week = result.scalar_one()
        return user, week
    return _make

@pytest.fixture(autouse=True)
def clear_price_cache():
    from services.pricing import price_cache
    from services.bingx_market_data import _price_cache as _bingx_cache
    price_cache.clear()
    price_cache.max_staleness = 3
    _bingx_cache.clear()
    yield
    price_cache.clear()
    _bingx_cache.clear()
