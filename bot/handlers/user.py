from decimal import Decimal
from datetime import datetime, timezone
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from db.models import User, Week, Transaction, Position, Asset, LeaderboardSnapshot
from services.accounts import get_or_create_user, accept_rules, verify_phone, verify_phone_and_grant
from services.trading import execute_buy, execute_sell, TradingError, InsufficientFunds, InsufficientPosition, StalePrice
from services.pricing import price_cache
from db.repo import get_cash_balance

router = Router()

@router.message(Command("start"))
async def cmd_start(message: Message, session: AsyncSession):
    from config import settings as _cfg
    if _cfg.trading_mode == "paper":
        # new Trading Game handles /start via profile router; keep legacy silent
        return
    user = await get_or_create_user(session, message.from_user.id, message.from_user.username)
    await session.commit()
    from bot.keyboards import contact_keyboard, rules_keyboard
    text = (
        "Добро пожаловать в TradeWeek!\n"
        "Каждую неделю вы получаете виртуальные доллары и торгуете криптой по реальным ценам BingX.\n"
        "Для начала примите правила и поделитесь номером телефона."
    )
    await message.answer(text, reply_markup=rules_keyboard())
    await message.answer("Нажмите кнопку чтобы поделиться номером:", reply_markup=contact_keyboard())

@router.callback_query(F.data == "accept_rules")
async def cb_accept_rules(callback: CallbackQuery, session: AsyncSession):
    result = await session.execute(select(User).where(User.telegram_id == callback.from_user.id))
    user = result.scalar_one_or_none()
    if not user:
        await callback.answer("Сначала /start")
        return
    await accept_rules(session, user)
    await session.commit()
    await callback.answer("Правила приняты!")
    await callback.message.answer("Правила приняты. Теперь поделитесь номером.")

@router.message(F.contact)
async def handle_contact(message: Message, session: AsyncSession):
    contact = message.contact
    if contact.user_id != message.from_user.id:
        await message.answer("Поделитесь своим номером.")
        return
    result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
    user = result.scalar_one_or_none()
    if not user:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)
    await verify_phone_and_grant(session, user, contact.phone_number)
    await session.commit()
    await message.answer(f"Номер {contact.phone_number} подтверждён! Баланс пополнен 10000. Можно торговать.")

@router.message(Command("rules"))
async def cmd_rules(message: Message):
    await message.answer(
        "Правила TradeWeek:\n"
        "- Лонг-онли, без плеча, только market-ордера\n"
        "- Для приза учитываются только quote-eligible монеты (объём > порога)\n"
        "- Телефон обязателен, один приз на номер\n"
        "- Топ-10 проверяется вручную"
    )

@router.message(Command("balance"))
async def cmd_balance(message: Message, session: AsyncSession):
    result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
    user = result.scalar_one_or_none()
    if not user:
        await message.answer("Сначала /start")
        return
    week_res = await session.execute(select(Week).where(Week.status == "active").order_by(Week.id.desc()).limit(1))
    week = week_res.scalar_one_or_none()
    if not week:
        await message.answer("Нет активной недели")
        return
    bal = await get_cash_balance(session, user.id, week.id)
    await message.answer(f"Баланс: {bal} USD\nНеделя {week.week_number} | до {week.ends_at}")

@router.message(Command("price"))
async def cmd_price(message: Message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /price <symbol> напр. BTC-USDT")
        return
    symbol = parts[1].upper()
    entry = price_cache.get(symbol)
    if not entry:
        await message.answer(f"Цена для {symbol} недоступна")
        return
    price, ts = entry
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    await message.answer(f"{symbol}: {price} (обновлено {age:.1f}с назад)")

@router.message(Command("buy"))
async def cmd_buy(message: Message, session: AsyncSession):
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("Использование: /buy <symbol> <usd>")
        return
    symbol = parts[1].upper()
    try:
        usd = Decimal(parts[2])
    except:
        await message.answer("Неверная сумма")
        return
    result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
    user = result.scalar_one_or_none()
    if not user:
        await message.answer("Сначала /start")
        return
    idempotency_key = f"tg:{message.message_id}:{message.date.timestamp()}"
    # better use update_id if available; fallback to message_id
    try:
        async with session.begin():
            order = await execute_buy(session, user, symbol, usd, idempotency_key)
        await session.commit()
        await message.answer(f"Куплено {symbol} на {usd} USD по цене {order.executed_price}, qty {order.qty}")
    except StalePrice as e:
        await session.rollback()
        await message.answer(f"Цена устарела: {e}")
    except InsufficientFunds as e:
        await session.rollback()
        await message.answer(f"Недостаточно средств: {e}")
    except TradingError as e:
        await session.rollback()
        await message.answer(f"Ошибка: {e}")
    except PermissionError as e:
        await session.rollback()
        await message.answer(f"Доступ запрещён: {e}")

@router.message(Command("sell"))
async def cmd_sell(message: Message, session: AsyncSession):
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("Использование: /sell <symbol> <qty|all>")
        return
    symbol = parts[1].upper()
    qty_str = parts[2]
    result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
    user = result.scalar_one_or_none()
    if not user:
        await message.answer("Сначала /start")
        return
    idempotency_key = f"tg:sell:{message.message_id}:{message.date.timestamp()}"
    try:
        async with session.begin():
            order = await execute_sell(session, user, symbol, qty_str, idempotency_key)
        await session.commit()
        await message.answer(f"Продано {symbol} qty {order.qty} по цене {order.executed_price}")
    except (StalePrice, InsufficientPosition, TradingError, PermissionError) as e:
        await session.rollback()
        await message.answer(f"Ошибка: {e}")

@router.message(Command("portfolio"))
async def cmd_portfolio(message: Message, session: AsyncSession):
    result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
    user = result.scalar_one_or_none()
    if not user:
        await message.answer("Сначала /start")
        return
    week_res = await session.execute(select(Week).where(Week.status == "active").order_by(Week.id.desc()).limit(1))
    week = week_res.scalar_one_or_none()
    if not week:
        await message.answer("Нет активной недели")
        return
    pos_res = await session.execute(select(Position, Asset).join(Asset, Position.asset_symbol == Asset.symbol).where(Position.user_id == user.id, Position.week_id == week.id, Position.qty > 0))
    lines = []
    total_pnl = Decimal("0")
    for pos, asset in pos_res.all():
        entry = price_cache.get(pos.asset_symbol)
        cur_price = entry[0] if entry else pos.avg_entry_price
        value = pos.qty * cur_price
        cost = pos.qty * pos.avg_entry_price
        pnl = value - cost
        total_pnl += pnl
        lines.append(f"{pos.asset_symbol}: qty {pos.qty} avg {pos.avg_entry_price} cur {cur_price} PnL {pnl:.2f}")
    if not lines:
        await message.answer("Позиций нет")
    else:
        lines.append(f"Total unrealized PnL: {total_pnl:.2f}")
        await message.answer("\n".join(lines))

async def cmd_legacy_history(message: Message, session: AsyncSession):
    result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
    user = result.scalar_one_or_none()
    if not user:
        await message.answer("Сначала /start")
        return
    tx_res = await session.execute(select(Transaction).where(Transaction.user_id == user.id).order_by(Transaction.id.desc()).limit(10))
    lines = []
    for tx in tx_res.scalars().all():
        lines.append(f"{tx.created_at} {tx.type} {tx.amount} bal {tx.balance_after}")
    await message.answer("\n".join(lines) if lines else "История пуста")

@router.message(Command("leaderboard"))
async def cmd_leaderboard(message: Message, session: AsyncSession):
    week_res = await session.execute(select(Week).where(Week.status == "active").order_by(Week.id.desc()).limit(1))
    week = week_res.scalar_one_or_none()
    if not week:
        # try closing week snapshot
        week_res = await session.execute(select(Week).order_by(Week.id.desc()).limit(1))
        week = week_res.scalar_one_or_none()
    if not week:
        await message.answer("Нет данных рейтинга")
        return
    # live equity: compute for each user
    # use snapshots if week closed else live
    if week.status == "closed":
        snap_res = await session.execute(select(LeaderboardSnapshot, User).join(User, LeaderboardSnapshot.user_id == User.id).where(LeaderboardSnapshot.week_id == week.id).order_by(LeaderboardSnapshot.rank).limit(10))
        lines = []
        for snap, user in snap_res.all():
            lines.append(f"{snap.rank}. {user.username or user.telegram_id} equity {snap.total_equity}")
        await message.answer("Рейтинг (снапшот):\n" + "\n".join(lines) if lines else "Рейтинг пуст")
    else:
        # live: sum cash + positions at current price (eligible only)
        result = await session.execute(select(Transaction.user_id).where(Transaction.week_id == week.id).distinct())
        user_ids = [r[0] for r in result.all()]
        equities = []
        for uid in user_ids:
            cash = await get_cash_balance(session, uid, week.id)
            pos_res = await session.execute(select(Position, Asset).join(Asset, Position.asset_symbol == Asset.symbol).where(Position.user_id == uid, Position.week_id == week.id, Position.qty > 0))
            pos_val = Decimal("0")
            for pos, asset in pos_res.all():
                if not asset.is_quote_eligible:
                    continue
                entry = price_cache.get(asset.symbol)
                price = entry[0] if entry else pos.avg_entry_price
                pos_val += pos.qty * price
            total = cash + pos_val
            urec = await session.execute(select(User).where(User.id == uid))
            u = urec.scalar_one()
            equities.append((u.username or str(u.telegram_id), total))
        equities.sort(key=lambda x: x[1], reverse=True)
        lines = [f"{i+1}. {name} {eq:.2f}" for i, (name, eq) in enumerate(equities[:10])]
        await message.answer("Live рейтинг:\n" + "\n".join(lines) if lines else "Рейтинг пуст")
