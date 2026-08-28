from __future__ import annotations

import logging
from decimal import Decimal

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncEngine

from config import settings
from db.competition_models import Competition, CompetitionPrize, LeaderboardSnapshot
from db.models import User

logger = logging.getLogger(__name__)


async def notify_competition_finished(engine: AsyncEngine, competition_id: int) -> None:
    """Best-effort result notifications after financial finalization commits."""
    if not settings.bot_token:
        logger.warning("Competition notification skipped: BOT_TOKEN is not configured")
        return
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        competition = await session.get(Competition, competition_id)
        if not competition:
            return
        rows = await session.execute(
            select(LeaderboardSnapshot, User)
            .join(User, User.id == LeaderboardSnapshot.user_id)
            .where(LeaderboardSnapshot.competition_id == competition_id)
            .order_by(LeaderboardSnapshot.rank)
        )
        prizes = await session.execute(
            select(CompetitionPrize).where(CompetitionPrize.competition_id == competition_id)
        )
        prize_by_rank = {prize.rank: prize.amount for prize in prizes.scalars().all()}
        bot = Bot(token=settings.bot_token)
        try:
            for snapshot, user in rows.all():
                if user.is_simulated or user.telegram_id <= 0:
                    continue
                prize = prize_by_rank.get(snapshot.rank)
                prize_line = f"🎁 Приз: ${Decimal(str(prize)):.2f}" if prize is not None else "В TOP 10 не вошёл."
                text = (
                    f"🏁 ТУРНИР ЗАВЕРШЁН\n\n{competition.name}\n\n"
                    f"Твой результат: #{snapshot.rank}\n"
                    f"📈 ROI: {snapshot.roi:+.2f}%\n"
                    f"💰 Equity: ${snapshot.equity:,.2f}\n"
                    f"{prize_line}\n\nСледующий турнир уже скоро 🚀"
                )
                try:
                    await bot.send_message(user.telegram_id, text)
                except Exception:
                    # Notification errors must never affect financial state.
                    logger.exception("Competition notification failed for user %s", user.id)
        finally:
            await bot.session.close()
