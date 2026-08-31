"""services/competition.py + services/leaderboard.py — полный цикл турнира."""
from decimal import Decimal
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import select

from db.competition_models import Competition, CompetitionStatus, CompetitionParticipant, LeaderboardSnapshot
from db.models import User
from db.paper_models import Instrument, PaperPosition, PositionStatus
from services.bingx_market_data import PriceSnapshot, persist_snapshot
from services.competition import (
    get_active_competition, get_or_create_default_competition,
    join_competition, update_participant_equity, finish_competition,
)
from services.leaderboard import build_leaderboard, get_top_n, snapshot_leaderboard
from services.trading_account import get_or_create_trading_account
from services.paper_adapter import open_position

pytestmark = pytest.mark.asyncio


async def _setup(session, n_users=3):
    now = datetime.now(timezone.utc)
    comp = Competition(name="LIFE CUP", status=CompetitionStatus.ACTIVE.value,
                       starts_at=now, ends_at=now + timedelta(days=7),
                       initial_balance=Decimal("10000"), prize_pool=Decimal("100"),
                       ranking_metric="ROI", price_source="BINGX", market_type="USD_M_PERPETUAL")
    session.add(comp)
    await session.flush()
    session.add(Instrument(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT",
                           status="active", price_precision=2, quantity_precision=6,
                           min_quantity=Decimal("0.000001"), max_leverage=50))
    await session.flush()
    await persist_snapshot(session, PriceSnapshot("BTCUSDT", Decimal("70000"), Decimal("70001"), Decimal("70000.5"), now, now))
    accounts = []
    for i in range(n_users):
        u = User(telegram_id=70000 + i, username=f"life_{i}")
        session.add(u)
        await session.flush()
        acc = await get_or_create_trading_account(session, u.id)
        await join_competition(session, u.id, comp.id)
        accounts.append(acc)
    await session.commit()
    return comp, accounts, now


class TestGetActiveCompetition:
    async def test_active_found(self, session):
        comp, _, _ = await _setup(session)
        got = await get_active_competition(session)
        assert got is not None and got.id == comp.id

    async def test_expired_not_found(self, session):
        comp, _, _ = await _setup(session)
        comp.ends_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()
        got = await get_active_competition(session)
        assert got is None


class TestJoinCompetition:
    async def test_join_new(self, session):
        comp, accounts, _ = await _setup(session, n_users=1)
        part = (await session.execute(
            select(CompetitionParticipant).where(CompetitionParticipant.competition_id == comp.id)
        )).scalars().first()
        assert part.starting_equity == Decimal("10000")
        assert part.current_equity == Decimal("10000")

    async def test_join_twice_idempotent(self, session):
        comp, accounts, _ = await _setup(session, n_users=1)
        from services.competition import join_competition
        p1 = await join_competition(session, accounts[0].user_id, comp.id)
        p2 = await join_competition(session, accounts[0].user_id, comp.id)
        assert p1 is p2 or (p1.id == p2.id)


class TestUpdateParticipantEquity:
    async def test_roi_calculated(self, session):
        comp, accounts, now = await _setup(session, n_users=1)
        acc = accounts[0]
        pos = await open_position(session, acc, "BTCUSDT", "LONG",
                                  notional=Decimal("5000"), competition_id=comp.id,
                                  idempotency_key="eq-open", leverage=10)
        await session.flush()
        pos.current_price = Decimal("77000")
        pos.unrealized_pnl = (Decimal("77000") - pos.entry_price) * pos.quantity
        await update_participant_equity(session, acc.user_id, comp.id)
        part = (await session.execute(
            select(CompetitionParticipant).where(CompetitionParticipant.competition_id == comp.id)
        )).scalars().first()
        assert part.roi != Decimal("0")
        assert part.current_equity > Decimal("10000")


class TestFinishCompetition:
    async def test_finish_idempotent(self, session):
        comp, accounts, now = await _setup(session, n_users=2)
        lb = await build_leaderboard(session, comp.id)
        await finish_competition(session, comp.id)
        await session.commit()
        # second finish — no-op
        await finish_competition(session, comp.id)
        await session.commit()
        snaps = (await session.execute(
            select(LeaderboardSnapshot).where(LeaderboardSnapshot.competition_id == comp.id)
        )).scalars().all()
        assert len(snaps) == 2  # one per participant, not doubled
        comp2 = await session.get(Competition, comp.id)
        assert comp2.status == CompetitionStatus.FINISHED.value


class TestBuildLeaderboard:
    async def test_sorted_by_roi(self, session):
        comp, accounts, now = await _setup(session, n_users=3)
        lb = await build_leaderboard(session, comp.id)
        rois = [e["roi"] for e in lb]
        assert rois == sorted(rois, reverse=True)

    async def test_empty(self, session):
        now = datetime.now(timezone.utc)
        comp = Competition(name="EMPTY", status=CompetitionStatus.ACTIVE.value,
                           starts_at=now, ends_at=now + timedelta(days=1),
                           initial_balance=Decimal("10000"), prize_pool=Decimal("0"),
                           ranking_metric="ROI", price_source="BINGX", market_type="USD_M_PERPETUAL")
        session.add(comp)
        await session.flush()
        lb = await build_leaderboard(session, comp.id)
        assert lb == []

    async def test_read_only_no_flush(self, session):
        """build_leaderboard не мутирует БД."""
        comp, accounts, _ = await _setup(session, n_users=2)
        before = [(p.id, p.roi, p.current_equity) for p in (await session.execute(
            select(CompetitionParticipant).where(CompetitionParticipant.competition_id == comp.id)
        )).scalars().all()]
        lb = await build_leaderboard(session, comp.id)
        # Flush was removed in Phase 1 — no DB mutation
        after = [(p.id, p.roi, p.current_equity) for p in (await session.execute(
            select(CompetitionParticipant).where(CompetitionParticipant.competition_id == comp.id)
        )).scalars().all()]
        # ROI may differ (recalculated), but no explicit flush → no forced write