from __future__ import annotations
from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from db.competition_models import CompetitionParticipant, LeaderboardSnapshot
from db.paper_models import TradingAccount, PaperPosition, PositionStatus
from services.metrics import increment

async def build_leaderboard(session: AsyncSession, competition_id: int) -> list[dict]:
    # Single-query fetch: participants + trading account + aggregated unrealized
    # Avoid N+1: one query with outer joins and group by
    from sqlalchemy import func

    # Fetch participants
    result = await session.execute(
        select(CompetitionParticipant).where(CompetitionParticipant.competition_id == competition_id)
    )
    participants = result.scalars().all()
    if not participants:
        return []

    # Batch fetch trading accounts for all participants
    user_ids = [p.user_id for p in participants]
    acc_res = await session.execute(select(TradingAccount).where(TradingAccount.user_id.in_(user_ids)))
    accounts_by_user = {acc.user_id: acc for acc in acc_res.scalars().all()}

    # Batch fetch unrealized sums per account
    account_ids = [acc.id for acc in accounts_by_user.values()]
    unrealized_by_account: dict[int, Decimal] = {}
    if account_ids:
        # Use coalesce to handle no positions
        q = await session.execute(
            select(PaperPosition.account_id, func.coalesce(func.sum(PaperPosition.unrealized_pnl), 0)).where(
                PaperPosition.account_id.in_(account_ids), PaperPosition.status == PositionStatus.OPEN.value
            ).group_by(PaperPosition.account_id)
        )
        for acc_id, total in q.all():
            unrealized_by_account[acc_id] = Decimal(str(total)) if total is not None else Decimal("0")

    # Compute w/o mutating DB (read-only) — calculate in memory
    computed = []
    for p in participants:
        acc = accounts_by_user.get(p.user_id)
        if acc:
            unrealized = unrealized_by_account.get(acc.id, Decimal("0"))
            current_equity = (acc.cash_balance + acc.margin_used + unrealized).quantize(Decimal("0.01"))
            realized = acc.realized_pnl
            if p.starting_equity != 0:
                roi = ((current_equity - p.starting_equity) / p.starting_equity * 100).quantize(Decimal("0.0001"))
            else:
                roi = Decimal("0")
        else:
            unrealized = Decimal("0")
            current_equity = p.current_equity
            realized = p.realized_pnl
            roi = p.roi
        computed.append((p, current_equity, realized, unrealized, roi))

    # Sort by ROI DESC, equity DESC, joined_at, user_id
    computed.sort(key=lambda x: (-x[4], -x[1], x[0].joined_at, x[0].user_id))

    leaderboard = []
    for idx, (p, current_equity, realized, unrealized, roi) in enumerate(computed, start=1):
        leaderboard.append({
            "rank": idx,
            "user_id": p.user_id,
            "roi": roi,
            "equity": current_equity,
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "joined_at": p.joined_at,
        })
    # No DB flush — read-only, avoids contention
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
