from __future__ import annotations
from decimal import Decimal
from datetime import datetime, timezone
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from db.models import Week, User, Transaction, Position, Asset, LeaderboardSnapshot, Prize, TransactionType, WeekStatus
from db.repo import get_cash_balance
from services.pricing import price_cache
import logging

logger = logging.getLogger(__name__)

async def get_or_create_active_week(session: AsyncSession) -> Week:
    result = await session.execute(select(Week).where(Week.status == WeekStatus.active.value).order_by(Week.id.desc()).limit(1))
    week = result.scalar_one_or_none()
    if week:
        return week
    # create first week
    now = datetime.now(timezone.utc)
    week = Week(week_number=1, starts_at=now, ends_at=now, status=WeekStatus.active.value)
    session.add(week)
    await session.flush()
    return week

async def grant_weekly(session: AsyncSession, user_id: int, week_id: int, amount: Decimal, idempotency_key: str):
    from sqlalchemy.exc import IntegrityError
    # UNIQUE constraint via idempotency_key + logical unique (partial index)
    existing_tx = await session.execute(select(Transaction).where(Transaction.idempotency_key == idempotency_key))
    if existing_tx.scalar_one_or_none() is not None:
        return
    # also check existing WEEKLY_GRANT for user/week
    existing_grant = await session.execute(
        select(Transaction).where(Transaction.user_id == user_id, Transaction.week_id == week_id, Transaction.type == TransactionType.WEEKLY_GRANT.value)
    )
    if existing_grant.scalar_one_or_none() is not None:
        return
    balance = await get_cash_balance(session, user_id, week_id)
    balance_after = (balance + amount).quantize(Decimal("0.01"))
    try:
        async with session.begin_nested():
            tx = Transaction(
                user_id=user_id, week_id=week_id, type=TransactionType.WEEKLY_GRANT.value,
                amount=amount, balance_after=balance_after, idempotency_key=idempotency_key
            )
            session.add(tx)
            await session.flush()
    except IntegrityError:
        # race: другой поток уже вставил WEEKLY_GRANT для этого (user,week) — partial unique сработал
        # begin_nested rolled back savepoint automatically, outer tx intact
        return
    # success — already flushed inside savepoint, no need to flush again (savepoint committed)

async def close_week(session: AsyncSession, week: Week, prize_top_n: int = 10, grant_amount: Decimal = Decimal("10000")):
    """
    Idempotent resumable weekly cycle per spec 5.6:
    a) status='closing', fix closing timestamp
    b) snapshots
    c) forced closes
    d) create new week
    e) grants
    f) status='closed'
    If fails mid-way, re-entry continues from current status.
    """
    now = datetime.now(timezone.utc)

    # Step a: mark closing
    if week.status == WeekStatus.active.value:
        week.status = WeekStatus.closing.value
        # we fix closing timestamp as now, stored in closed_at initially? Use closed_at as closing_price ts for now
        # Actually closed_at set at final close, but we need closing timestamp for prices.
        # We'll use `closed_at` as closing price timestamp if not set.
        if week.closed_at is None:
            week.closed_at = now  # will be overwritten at final close but keep initial for price snapshot
        await session.flush()

    closing_ts = week.closed_at or now

    # Step b: snapshots - if not already created
    existing_snaps = await session.execute(select(func.count()).select_from(LeaderboardSnapshot).where(LeaderboardSnapshot.week_id == week.id))
    snap_count = existing_snaps.scalar_one()
    if snap_count == 0:
        # need to compute equity for each user with any transaction in week
        result = await session.execute(select(Transaction.user_id).where(Transaction.week_id == week.id).distinct())
        user_ids = [r[0] for r in result.all()]
        # also include users with positions but no transactions? include all users with positions
        pos_users = await session.execute(select(Position.user_id).where(Position.week_id == week.id).distinct())
        for r in pos_users.all():
            if r[0] not in user_ids:
                user_ids.append(r[0])

        # Build equity list
        equities = []
        for uid in user_ids:
            cash = await get_cash_balance(session, uid, week.id)
            # positions value only for quote_eligible assets
            pos_result = await session.execute(
                select(Position, Asset).join(Asset, Position.asset_symbol == Asset.symbol).where(
                    Position.user_id == uid, Position.week_id == week.id, Position.qty > 0
                )
            )
            pos_value = Decimal("0")
            for pos, asset in pos_result.all():
                if not asset.is_quote_eligible:
                    continue
                # get closing price
                price_entry = price_cache.get(asset.symbol)
                if price_entry:
                    price, _ = price_entry
                else:
                    # fallback to avg_entry_price if no price
                    price = pos.avg_entry_price
                pos_value += (pos.qty * price).quantize(Decimal("0.01"))
            total = (cash + pos_value).quantize(Decimal("0.01"))
            equities.append((uid, cash, pos_value, total))

        # sort by total descending
        equities.sort(key=lambda x: x[3], reverse=True)
        for idx, (uid, cash, pos_val, total) in enumerate(equities, start=1):
            snap = LeaderboardSnapshot(
                week_id=week.id, user_id=uid, rank=idx,
                cash_balance=cash, positions_value=pos_val, total_equity=total
            )
            session.add(snap)
        await session.flush()

        # create prizes for top N
        for snap in await session.execute(select(LeaderboardSnapshot).where(LeaderboardSnapshot.week_id == week.id, LeaderboardSnapshot.rank <= prize_top_n)):
            s = snap.scalar_one() if hasattr(snap, "scalar_one") else snap[0]
            # handle both
            try:
                obj = s
                if isinstance(s, LeaderboardSnapshot):
                    obj = s
                else:
                    obj = s[0]
            except:
                continue
        # simpler: reload
        result2 = await session.execute(select(LeaderboardSnapshot).where(LeaderboardSnapshot.week_id == week.id, LeaderboardSnapshot.rank <= prize_top_n))
        for snap in result2.scalars().all():
            existing_prize = await session.execute(select(Prize).where(Prize.week_id == week.id, Prize.rank == snap.rank))
            if existing_prize.scalar_one_or_none() is None:
                prize = Prize(week_id=week.id, rank=snap.rank, user_id=snap.user_id, description=f"Top {snap.rank} prize week {week.week_number}")
                session.add(prize)
        await session.flush()
    # else snapshots already exist, skip

    # Step c: forced closes - only after snapshots
    # Check if forced close transactions already exist for week
    # We close each position at closing price
    pos_result = await session.execute(select(Position).where(Position.week_id == week.id, Position.qty > 0))
    positions = pos_result.scalars().all()
    for pos in positions:
        # check if already closed via transaction idempotency
        tx_key = f"forced_close:{week.id}:{pos.user_id}:{pos.asset_symbol}"
        existing = await session.execute(select(Transaction).where(Transaction.idempotency_key == tx_key))
        if existing.scalar_one_or_none() is not None:
            continue
        # get closing price
        price_entry = price_cache.get(pos.asset_symbol)
        if price_entry:
            price, price_ts = price_entry
        else:
            price = pos.avg_entry_price
            price_ts = closing_ts
        proceeds = (pos.qty * price).quantize(Decimal("0.01"))
        cash_before = await get_cash_balance(session, pos.user_id, week.id)
        balance_after = (cash_before + proceeds).quantize(Decimal("0.01"))
        tx = Transaction(
            user_id=pos.user_id, week_id=week.id, type=TransactionType.FORCED_CLOSE.value,
            amount=proceeds, balance_after=balance_after, idempotency_key=tx_key
        )
        session.add(tx)
        # zero position
        pos.qty = Decimal("0")
        pos.updated_at = datetime.now(timezone.utc)
    await session.flush()

    # Step d: create new week
    result_new = await session.execute(select(Week).where(Week.week_number == week.week_number + 1))
    new_week = result_new.scalar_one_or_none()
    if new_week is None:
        new_week = Week(
            week_number=week.week_number + 1,
            starts_at=now,
            ends_at=now,  # placeholder
            status=WeekStatus.active.value
        )
        session.add(new_week)
        await session.flush()
    # else exists, reuse

    # Step e: grants for new week
    # active users = not banned and phone_verified?
    # Spec says each active (not banned) user. We'll grant to all not banned.
    users_result = await session.execute(select(User).where(User.is_banned == False))
    for user in users_result.scalars().all():
        key = f"grant:{new_week.id}:{user.id}"
        # check if already granted
        existing = await session.execute(select(Transaction).where(Transaction.idempotency_key == key))
        if existing.scalar_one_or_none() is not None:
            continue
        # also check unique per user/week
        existing2 = await session.execute(
            select(Transaction).where(Transaction.user_id == user.id, Transaction.week_id == new_week.id, Transaction.type == TransactionType.WEEKLY_GRANT.value)
        )
        if existing2.scalar_one_or_none() is not None:
            continue
        tx = Transaction(
            user_id=user.id, week_id=new_week.id, type=TransactionType.WEEKLY_GRANT.value,
            amount=grant_amount, balance_after=grant_amount, idempotency_key=key
        )
        session.add(tx)
    await session.flush()

    # Step f: mark old week closed
    if week.status != WeekStatus.closed.value:
        week.status = WeekStatus.closed.value
        week.closed_at = now
        await session.flush()

    return new_week
