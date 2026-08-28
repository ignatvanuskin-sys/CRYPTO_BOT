from __future__ import annotations
from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from db.competition_models import CompetitionParticipant, LeaderboardSnapshot
from db.paper_models import TradingAccount, PaperPosition, PositionStatus
from services.metrics import increment

async def build_leaderboard(session: AsyncSession, competition_id: int) -> list[dict]:
    result = await session.execute(select(CompetitionParticipant).where(CompetitionParticipant.competition_id == competition_id))
    participants = result.scalars().all()
    # update equity for each from trading account + positions
    for p in participants:
        acc_res = await session.execute(select(TradingAccount).where(TradingAccount.user_id == p.user_id))
        acc = acc_res.scalar_one_or_none()
        if acc:
            # recalc unrealized from positions
            q = await session.execute(select(PaperPosition).where(PaperPosition.account_id == acc.id, PaperPosition.status == PositionStatus.OPEN.value))
            unrealized = sum((pos.unrealized_pnl for pos in q.scalars().all()), Decimal("0"))
            p.unrealized_pnl = unrealized
            p.realized_pnl = acc.realized_pnl
            p.current_equity = (acc.cash_balance + acc.margin_used + unrealized).quantize(Decimal("0.01"))
            if p.starting_equity != 0:
                p.roi = ((p.current_equity - p.starting_equity) / p.starting_equity * 100).quantize(Decimal("0.0001"))
            else:
                p.roi = Decimal("0")
    await session.flush()
    # sort: ROI DESC, equity DESC, fewer trades, earlier joined
    # need trade count
    sorted_parts = sorted(
        participants,
        key=lambda x: (-x.roi, -x.current_equity, x.joined_at, x.user_id),
    )
    leaderboard = []
    for idx, p in enumerate(sorted_parts, start=1):
        p.rank = idx
        leaderboard.append({
            "rank": idx,
            "user_id": p.user_id,
            "roi": p.roi,
            "equity": p.current_equity,
            "realized_pnl": p.realized_pnl,
            "unrealized_pnl": p.unrealized_pnl,
            "joined_at": p.joined_at,
        })
    await session.flush()
    return leaderboard

async def get_top_n(session: AsyncSession, competition_id: int, n: int = 10) -> list[dict]:
    increment("leaderboard_viewed")
    lb = await build_leaderboard(session, competition_id)
    return lb[:n]

async def get_user_rank(session: AsyncSession, competition_id: int, user_id: int) -> dict | None:
    lb = await build_leaderboard(session, competition_id)
    for entry in lb:
        if entry["user_id"] == user_id:
            # also compute need to top 10
            top10_roi = lb[9]["roi"] if len(lb) >= 10 else None
            need = (top10_roi - entry["roi"]).quantize(Decimal("0.01")) if top10_roi is not None else None
            entry["need_for_top10"] = need
            return entry
    return None

async def snapshot_leaderboard(session: AsyncSession, competition_id: int, leaderboard: list[dict]):
    for entry in leaderboard:
        snap = LeaderboardSnapshot(
            competition_id=competition_id,
            user_id=entry["user_id"],
            rank=entry["rank"],
            equity=entry["equity"],
            roi=entry["roi"],
            realized_pnl=entry["realized_pnl"],
            unrealized_pnl=entry["unrealized_pnl"],
        )
        session.add(snap)
    await session.flush()
