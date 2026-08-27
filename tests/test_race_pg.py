"""
Реальная гонка на Postgres (asyncpg). Требует pg_engine (testcontainers).
На aiosqlite этот тест НЕВАЛИДЕН и пропускается — см. conftest.py:pg_engine.

Гарантия перекрытия:
- Корутина A захватывает FOR UPDATE на users.id, затем держит транзакцию
  открытой 0.3с (asyncio.sleep) между чтением баланса и списанием.
- Корутина B стартует сразу (asyncio.gather) и гарантированно упирается
  в заблокированную строку users до коммита A (PG блокировка).
- Без такой задержки asyncio.gather мог бы выполнить A целиком до старта B
  (последовательно), и тест был бы ложно-зелёным.

Если Docker недоступен — тест скипается (pytest.skip), но в CI с Docker
должен проходить и доказывать, что ровно один buy проходит.
"""
import pytest
import asyncio
from decimal import Decimal
from datetime import datetime, timezone
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from db.models import Transaction, Order, User, Week, Asset
from services.pricing import price_cache

pytestmark = pytest.mark.asyncio

async def test_parallel_buy_real_race_pg(pg_engine, make_pg_user_week):
    """Engine: postgresql+asyncpg — два параллельных buy на весь баланс, ровно один проходит."""
    user, week = await make_pg_user_week(telegram_id=1, grant=Decimal("10000"))
    price_cache.update("BTC-USDT", Decimal("50000"), datetime.now(timezone.utc))
    # ensure asset
    factory = async_sessionmaker(pg_engine, expire_on_commit=False)

    # instrument: patch lock to hold for 0.3s to guarantee overlap
    from db import repo as repo_mod
    orig_lock = repo_mod.lock_user_week
    async def delayed_lock(session, uid, wid):
        await orig_lock(session, uid, wid)
        await asyncio.sleep(0.3)
    repo_mod.lock_user_week = delayed_lock

    from services.trading import execute_buy

    async def buy_one(key, amount):
        async with factory() as s:
            res = await s.execute(select(User).where(User.id == user.id))
            u = res.scalar_one()
            try:
                await execute_buy(s, u, "BTC-USDT", amount, key)
                await s.commit()
                return "ok"
            except Exception as e:
                await s.rollback()
                return f"err:{type(e).__name__}:{e}"

    try:
        # две корутины стартуют одновременно, B гарантированно попадает в блокировку A
        r1, r2 = await asyncio.gather(buy_one("race-k1", Decimal("6000")), buy_one("race-k2", Decimal("6000")))
    finally:
        repo_mod.lock_user_week = orig_lock

    # один ok, второй InsufficientFunds / err
    oks = [r for r in (r1, r2) if r == "ok"]
    errs = [r for r in (r1, r2) if r != "ok"]
    assert len(oks) == 1, f"expected exactly 1 ok, got {r1}, {r2}"
    assert len(errs) == 1 and "InsufficientFunds" in errs[0], f"expected InsufficientFunds, got {errs}"

    # баланс не ушёл в минус
    async with factory() as s:
        res = await s.execute(select(func.coalesce(func.sum(Transaction.amount), 0)).where(Transaction.user_id == user.id, Transaction.week_id == week.id))
        total = Decimal(str(res.scalar_one()))
        assert total >= 0, "balance negative after race"
        # ровно один filled
        res2 = await s.execute(select(func.count()).select_from(Order).where(Order.status == "filled", Order.user_id == user.id))
        assert res2.scalar_one() == 1

async def test_parallel_buy_sell_no_deadlock_pg(pg_engine, make_pg_user_week):
    """
    Engine: postgresql+asyncpg — параллельные buy/sell по разным активам одного юзера
    не дедлочатся. Доказательство: все операции лочат ровно одну строку users.id
    в одинаковом порядке (см. db/repo.py:lock_user_week), поэтому нет цикла ожидания.
    Таймаут 5с — если бы был дедлок, gather повис бы.
    """
    user, week = await make_pg_user_week(telegram_id=2, grant=Decimal("10000"))
    price_cache.update("BTC-USDT", Decimal("50000"), datetime.now(timezone.utc))
    price_cache.update("ETH-USDT", Decimal("3000"), datetime.now(timezone.utc))
    # seed position for sell
    from services.trading import execute_buy, execute_sell
    async with async_sessionmaker(pg_engine, expire_on_commit=False)() as s:
        res = await s.execute(select(User).where(User.id == user.id))
        u = res.scalar_one()
        await execute_buy(s, u, "ETH-USDT", Decimal("3000"), "seed-eth")
        await s.commit()

    factory = async_sessionmaker(pg_engine, expire_on_commit=False)

    async def do_buy():
        async with factory() as s:
            res = await s.execute(select(User).where(User.id == user.id))
            u = res.scalar_one()
            try:
                await execute_buy(s, u, "BTC-USDT", Decimal("1000"), "deadlock-buy")
                await s.commit()
                return "ok-buy"
            except Exception as e:
                await s.rollback()
                return f"err:{e}"

    async def do_sell():
        async with factory() as s:
            res = await s.execute(select(User).where(User.id == user.id))
            u = res.scalar_one()
            try:
                await execute_sell(s, u, "ETH-USDT", Decimal("0.5"), "deadlock-sell")
                await s.commit()
                return "ok-sell"
            except Exception as e:
                await s.rollback()
                return f"err:{e}"

    # оба лочат users.id — порядок одинаковый, дедлока нет
    results = await asyncio.wait_for(asyncio.gather(do_buy(), do_sell()), timeout=5.0)
    # оба должны успеть (возможно один отработает после другого, но не повиснуть)
    assert len(results) == 2
    assert all(r.startswith("ok") or "Insufficient" in r for r in results)

async def test_check_constraint_pg(pg_engine, make_pg_user_week):
    """Engine: postgresql+asyncpg — CHECK balance_after >=0 на уровне БД (pg partial index тоже)."""
    user, week = await make_pg_user_week(telegram_id=3, grant=Decimal("100"))
    from db.models import Transaction as Tx
    async with async_sessionmaker(pg_engine, expire_on_commit=False)() as s:
        tx = Tx(user_id=user.id, week_id=week.id, type="ADJUSTMENT", amount=Decimal("-200"), balance_after=Decimal("-100"), idempotency_key="pg-neg-check")
        s.add(tx)
        with pytest.raises(Exception):
            await s.commit()
        await s.rollback()

async def test_weekly_grant_unique_pg(pg_engine, make_pg_user_week):
    """Engine: postgresql+asyncpg — UNIQUE (user_id, week_id) WHERE type=WEEKLY_GRANT невозможен дубль даже при race."""
    user, week = await make_pg_user_week(telegram_id=4, grant=None)
    from services.weekly_cycle import grant_weekly
    factory = async_sessionmaker(pg_engine, expire_on_commit=False)
    async def grant(k):
        async with factory() as s:
            await grant_weekly(s, user.id, week.id, Decimal("10000"), k)
            await s.commit()
    # два параллельных гранта с разными idempotency_keys но same (user,week) — второй должен быть no-op из-за логики + constraint
    await asyncio.gather(grant(f"grant:{week.id}:{user.id}:a"), grant(f"grant:{week.id}:{user.id}:b"))
    async with factory() as s:
        res = await s.execute(select(func.count()).select_from(Transaction).where(Transaction.type == "WEEKLY_GRANT", Transaction.user_id == user.id))
        assert res.scalar_one() == 1
