"""
Денежные тесты на aiosqlite (допустимо только для не-конкурентных проверок).
Для КАЖДОГО теста указан движок в docstring.

Важно: тесты на гонку/ FOR UPDATE здесь — ТОЛЬКО как smoke на sqlite,
реальная проверка — в tests/test_race_pg.py на реальном Postgres (asyncpg).
Если pg_engine недоступен (нет Docker), тот файл скипается, но здесь
тесты остаются зелёными как baseline.

Engine: aiosqlite (sqlite_engine fixture)
"""
import pytest
import asyncio
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import async_sessionmaker
from db.models import Transaction, Order, Position, Week, User, Asset, LeaderboardSnapshot, Prize
from services.pricing import price_cache
from services.trading import execute_buy, execute_sell, InsufficientFunds, InsufficientPosition, StalePrice, TradingError, verify_balances
from services.weekly_cycle import close_week, grant_weekly
from services.accounts import get_or_create_user

pytestmark = pytest.mark.asyncio

async def test_idempotency_no_double(session, make_user_week):
    """Engine: aiosqlite — идемпотентность по UNIQUE(idempotency_key), не требует FOR UPDATE."""
    user, week = await make_user_week(telegram_id=10, grant=Decimal("10000"))
    price_cache.update("BTC-USDT", Decimal("50000"), datetime.now(timezone.utc))
    await execute_buy(session, user, "BTC-USDT", Decimal("1000"), "idem-key-1")
    await session.commit()
    order = await execute_buy(session, user, "BTC-USDT", Decimal("1000"), "idem-key-1")
    await session.commit()
    result = await session.execute(select(func.count()).select_from(Transaction).where(Transaction.user_id == user.id))
    assert result.scalar_one() == 2
    res2 = await session.execute(select(func.count()).select_from(Order).where(Order.idempotency_key == "idem-key-1"))
    assert res2.scalar_one() == 1

async def test_double_grant_once(session, make_user_week):
    """Engine: aiosqlite — двойное начисление WEEKLY_GRANT, проверка UNIQUE constraint логики (но реальный partial index только на PG)."""
    user, week = await make_user_week(telegram_id=20, grant=None)
    await grant_weekly(session, user.id, week.id, Decimal("10000"), f"grant:{week.id}:{user.id}")
    await session.commit()
    await grant_weekly(session, user.id, week.id, Decimal("10000"), f"grant:{week.id}:{user.id}")
    await session.commit()
    res = await session.execute(select(func.count()).select_from(Transaction).where(Transaction.type == "WEEKLY_GRANT", Transaction.user_id == user.id, Transaction.week_id == week.id))
    assert res.scalar_one() == 1
    price_cache.update("BTC-USDT", Decimal("50000"), datetime.now(timezone.utc))
    await close_week(session, week, prize_top_n=10, grant_amount=Decimal("10000"))
    await session.commit()
    await close_week(session, week, prize_top_n=10, grant_amount=Decimal("10000"))
    await session.commit()
    new_week_res = await session.execute(select(Week).where(Week.week_number == 2))
    new_week = new_week_res.scalar_one()
    res2 = await session.execute(select(func.count()).select_from(Transaction).where(Transaction.type == "WEEKLY_GRANT", Transaction.week_id == new_week.id))
    assert res2.scalar_one() == 1

async def test_crash_between_steps_resumable(session, make_user_week):
    """Engine: aiosqlite — resumable weekly cycle без проверки блокировок."""
    user, week = await make_user_week(telegram_id=30, grant=Decimal("10000"))
    price_cache.update("BTC-USDT", Decimal("50000"), datetime.now(timezone.utc))
    await execute_buy(session, user, "BTC-USDT", Decimal("1000"), "buy-crash")
    await session.commit()
    await close_week(session, week, prize_top_n=10, grant_amount=Decimal("10000"))
    await session.commit()
    snap_cnt = (await session.execute(select(func.count()).select_from(LeaderboardSnapshot).where(LeaderboardSnapshot.week_id == week.id))).scalar_one()
    assert snap_cnt >= 1
    await close_week(session, week, prize_top_n=10, grant_amount=Decimal("10000"))
    await session.commit()
    snap_cnt2 = (await session.execute(select(func.count()).select_from(LeaderboardSnapshot).where(LeaderboardSnapshot.week_id == week.id))).scalar_one()
    assert snap_cnt2 == snap_cnt
    pos_res = await session.execute(select(Position).where(Position.week_id == week.id))
    for p in pos_res.scalars().all():
        assert p.qty == Decimal("0")

async def test_stale_price_rejected(session, make_user_week):
    """Engine: aiosqlite — отказ при протухшей цене MAX_PRICE_STALENESS_SECONDS."""
    user, week = await make_user_week(telegram_id=40, grant=Decimal("10000"))
    price_cache.update("BTC-USDT", Decimal("50000"), datetime.now(timezone.utc) - timedelta(seconds=10))
    with pytest.raises(StalePrice):
        await execute_buy(session, user, "BTC-USDT", Decimal("100"), "stale-key")
        await session.commit()
    await session.commit()
    res = await session.execute(select(func.count()).select_from(Order).where(Order.status == "rejected"))
    assert res.scalar_one() >= 1

async def test_sell_more_than_position(session, make_user_week):
    """Engine: aiosqlite — sell больше позиции."""
    user, week = await make_user_week(telegram_id=50, grant=Decimal("10000"))
    price_cache.update("BTC-USDT", Decimal("50000"), datetime.now(timezone.utc))
    await execute_buy(session, user, "BTC-USDT", Decimal("1000"), "buy-sell-test")
    await session.commit()
    with pytest.raises(InsufficientPosition):
        await execute_sell(session, user, "BTC-USDT", Decimal("100"), "sell-too-much")
    await session.rollback()

async def test_buy_more_than_balance(session, make_user_week):
    """Engine: aiosqlite — buy больше баланса."""
    user, week = await make_user_week(telegram_id=60, grant=Decimal("100"))
    price_cache.update("BTC-USDT", Decimal("50000"), datetime.now(timezone.utc))
    with pytest.raises(InsufficientFunds):
        await execute_buy(session, user, "BTC-USDT", Decimal("1000"), "buy-too-much")
    await session.rollback()

async def test_snapshot_single_closing_timestamp(session, make_user_week):
    """Engine: aiosqlite — единый closing timestamp."""
    user1, week = await make_user_week(telegram_id=70, grant=Decimal("10000"))
    from db.models import User as U
    u2 = U(telegram_id=71, username="u2", phone_number="+79000000071", phone_verified_at=datetime.now(timezone.utc), rules_accepted_at=datetime.now(timezone.utc))
    session.add(u2)
    await session.flush()
    from db.models import Transaction as Tx
    tx2 = Tx(user_id=u2.id, week_id=week.id, type="WEEKLY_GRANT", amount=Decimal("10000"), balance_after=Decimal("10000"), idempotency_key=f"grant:{week.id}:{u2.id}")
    session.add(tx2)
    await session.commit()
    price_cache.update("BTC-USDT", Decimal("50000"), datetime.now(timezone.utc))
    await execute_buy(session, user1, "BTC-USDT", Decimal("5000"), "buy-snap1")
    await session.commit()
    res = await session.execute(select(U).where(U.id == u2.id))
    u2_fresh = res.scalar_one()
    await execute_buy(session, u2_fresh, "BTC-USDT", Decimal("2000"), "buy-snap2")
    await session.commit()
    await close_week(session, week, prize_top_n=10, grant_amount=Decimal("10000"))
    await session.commit()
    snaps = (await session.execute(select(LeaderboardSnapshot).where(LeaderboardSnapshot.week_id == week.id).order_by(LeaderboardSnapshot.rank))).scalars().all()
    assert len(snaps) >= 2
    times = [s.created_at for s in snaps]
    delta = max(times) - min(times)
    assert delta.total_seconds() < 2

async def test_verify_finds_mismatch(session, make_user_week):
    """Engine: aiosqlite — сверка находит расхождение balance_after vs SUM."""
    user, week = await make_user_week(telegram_id=80, grant=Decimal("10000"))
    mism = await verify_balances(session, user_ids=[user.id], week_id=week.id)
    assert mism == []
    from db.models import Transaction as Tx
    tx = Tx(user_id=user.id, week_id=week.id, type="ADJUSTMENT", amount=Decimal("100"), balance_after=Decimal("999999"), idempotency_key="mismatch-test")
    session.add(tx)
    await session.commit()
    mism2 = await verify_balances(session, user_ids=[user.id], week_id=week.id)
    assert len(mism2) >= 1

async def test_trade_without_phone_rejected(session, sqlite_engine):
    """Engine: aiosqlite — торговля без phone_verified_at (сервисный слой)."""
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as s:
        async with s.begin():
            user = User(telegram_id=90, username="nophone")
            user.phone_verified_at = None
            user.rules_accepted_at = datetime.now(timezone.utc)
            s.add(user)
            await s.flush()
            week = Week(week_number=1, starts_at=datetime.now(timezone.utc), ends_at=datetime.now(timezone.utc), status="active")
            s.add(week)
            await s.flush()
            asset = Asset(symbol="BTC-USDT", base_asset="BTC", quote_asset="USDT", status="active", is_quote_eligible=True, last_24h_quote_volume=Decimal("2000000"))
            s.add(asset)
        await s.commit()
        price_cache.update("BTC-USDT", Decimal("50000"), datetime.now(timezone.utc))
        res = await s.execute(select(User).where(User.telegram_id == 90))
        u = res.scalar_one()
        with pytest.raises(PermissionError):
            await execute_buy(s, u, "BTC-USDT", Decimal("100"), "nophone-key")

async def test_banned_rejected(session, make_user_week):
    """Engine: aiosqlite — забаненный юзер."""
    user, week = await make_user_week(telegram_id=100, grant=Decimal("10000"))
    user.is_banned = True
    user.ban_reason = "cheat"
    await session.commit()
    price_cache.update("BTC-USDT", Decimal("50000"), datetime.now(timezone.utc))
    with pytest.raises(PermissionError):
        await execute_buy(session, user, "BTC-USDT", Decimal("100"), "banned-key")

async def test_non_eligible_not_in_snapshot(session, make_user_week):
    """Engine: aiosqlite — is_quote_eligible=False не попадает в рейтинг."""
    user, week = await make_user_week(telegram_id=110, grant=Decimal("10000"), eligible=True)
    price_cache.update("BTC-USDT", Decimal("50000"), datetime.now(timezone.utc))
    price_cache.update("SHIT-USDT", Decimal("0.001"), datetime.now(timezone.utc))
    await execute_buy(session, user, "SHIT-USDT", Decimal("5000"), "buy-shit")
    await session.commit()
    await execute_buy(session, user, "BTC-USDT", Decimal("1000"), "buy-btc-eligible")
    await session.commit()
    await close_week(session, week, prize_top_n=10, grant_amount=Decimal("10000"))
    await session.commit()
    snap_res = await session.execute(select(LeaderboardSnapshot).where(LeaderboardSnapshot.week_id == week.id, LeaderboardSnapshot.user_id == user.id))
    snap = snap_res.scalar_one()
    assert snap.positions_value == Decimal("1000.00") or snap.positions_value == Decimal("1000")
    assert snap.cash_balance == Decimal("4000.00") or snap.cash_balance == Decimal("4000")

async def test_balance_never_negative_constraint(session, make_user_week):
    """Engine: aiosqlite — CHECK balance_after >=0 (на sqlite тоже работает, но на PG — критично)."""
    user, week = await make_user_week(telegram_id=120, grant=Decimal("10000"))
    from db.models import Transaction as Tx
    tx = Tx(user_id=user.id, week_id=week.id, type="ADJUSTMENT", amount=Decimal("-20000"), balance_after=Decimal("-10000"), idempotency_key="neg-test")
    session.add(tx)
    with pytest.raises(Exception):
        await session.commit()
    await session.rollback()

async def test_sell_all(session, make_user_week):
    """Engine: aiosqlite — sell all."""
    user, week = await make_user_week(telegram_id=130, grant=Decimal("10000"))
    price_cache.update("BTC-USDT", Decimal("50000"), datetime.now(timezone.utc))
    await execute_buy(session, user, "BTC-USDT", Decimal("2000"), "buy-all-test")
    await session.commit()
    await execute_sell(session, user, "BTC-USDT", "all", "sell-all-key")
    await session.commit()
    pos = await session.execute(select(Position).where(Position.user_id == user.id, Position.week_id == week.id, Position.asset_symbol == "BTC-USDT"))
    p = pos.scalar_one_or_none()
    assert p is not None
    assert p.qty == Decimal("0") or p.qty == Decimal("0.0000000000")
