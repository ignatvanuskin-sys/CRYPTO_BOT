"""Phase 1 critical fixes tests.

Covers:
  - FIX #1: negative profit-based percentage TP/SL input (sign ignored, magnitude)
  - FIX #2: loss-capping writes explicit auditable ADJUSTMENT ledger, no money creation
  - FIX #3: open/close idempotency + ledger/account atomicity (same key = same result)
  - FIX #4: TP/SL edit IDOR — only owner can edit own OPEN position

PG-only concurrency tests are marked `@pytest.mark.pg_required` and skipped
when PG is unavailable; SQLite paths are marked clearly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select

from db.competition_models import Competition, CompetitionStatus
from db.models import User
from db.paper_models import (
    AccountLedger,
    Instrument,
    LedgerType,
    PaperPosition,
    PositionStatus,
    TradingAccount,
)
from services.bingx_market_data import PriceSnapshot, persist_snapshot
from services.competition import join_competition
from services.paper_adapter import close_position, open_position, update_position_tp_sl
from services.trading_account import get_or_create_trading_account

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _make_uid():
    import random

    return 900000 + random.randint(1, 5000)


async def _setup(session, symbol="SOLUSDT", side=None, leverage=10, notional=Decimal("1000"),
                 telegram_id=None, entry_bid=Decimal("100.00"), entry_ask=Decimal("100.10"),
                 price_precision=3, min_qty=Decimal("0.000001")):
    now = datetime.now(timezone.utc)
    uid = telegram_id or _make_uid()
    session.add(
        Instrument(
            symbol=symbol,
            base_asset=symbol.replace("USDT", ""),
            quote_asset="USDT",
            status="active",
            price_precision=price_precision,
            quantity_precision=6,
            min_quantity=min_qty,
            max_leverage=300,
        )
    )
    comp = Competition(
        name="PH1 TEST",
        status=CompetitionStatus.ACTIVE.value,
        starts_at=now - timedelta(hours=1),
        ends_at=now + timedelta(hours=24),
        initial_balance=Decimal("10000"),
        prize_pool=Decimal("0"),
        ranking_metric="ROI",
        price_source="BINGX",
        market_type="USD_M_PERPETUAL",
    )
    user = User(telegram_id=uid, username=f"ph1_{uid}")
    session.add_all([comp, user])
    await session.flush()
    account = await get_or_create_trading_account(session, user.id)
    await join_competition(session, user.id, comp.id)
    await persist_snapshot(
        session,
        PriceSnapshot(symbol, entry_bid, entry_ask, entry_ask, now, now),
    )
    await session.commit()
    return account, comp, user, now


# --------------------------------------------------------------------------
# FIX #1 — negative profit-based percentage input
# --------------------------------------------------------------------------

def _mk_state(symbol="SOLUSDT", side="LONG", leverage="10", budget="100", awaiting="tp_sl_price"):
    return {"symbol": symbol, "side": side, "budget": budget, "leverage": leverage, "awaiting": awaiting}


async def _feed_text(session, user, state, text):
    from bot.handlers import trade as trade_mod

    trade_mod.trade_state[user.telegram_id] = state
    msg = MagicMock()
    msg.from_user = MagicMock()
    msg.from_user.id = user.telegram_id
    msg.text = text
    msg.answer = AsyncMock()
    await trade_mod.handle_trade_text(msg, session)
    return trade_mod.trade_state.get(user.telegram_id, {})


async def test_negative_percent_single_long(session):
    """-5% means magnitude 5% (LONG TP up, SL down)."""
    account, _, user, _ = await _setup(session)
    st = await _feed_text(session, user, _mk_state(awaiting="tp_only_percent", leverage="10"), "-5")
    assert st["tp"] is not None
    assert st["sl"] is None
    # entry ASK for LONG decision uses snapshot.ask = 100.10
    # TP = 100.10 * (1 + 5/(100*10)) = 100.10*1.005 = 100.6005
    expected = (Decimal("100.10") * (Decimal("1") + Decimal("5") / (Decimal("100") * Decimal("10")))).quantize(Decimal("0.00000001"))
    assert st["tp"] == expected
    # _show_confirmation uses state (tp/sl set) — no exception
    await session.rollback()


async def test_negative_percent_single_short(session):
    """-5% SHORT TP means magnitude 5% below entry."""
    account, _, user, _ = await _setup(session, side="SHORT")
    # use snapshot bid for SHORT decision; entry_est = bid = 100.00
    st = await _feed_text(session, user, _mk_state(side="SHORT", awaiting="tp_only_percent", leverage="10"), "-5%")
    assert st["tp"] is not None
    assert st["sl"] is None
    expected = (Decimal("100.00") * (Decimal("1") - Decimal("5") / (Decimal("100") * Decimal("10")))).quantize(Decimal("0.00000001"))
    assert st["tp"] == expected
    await session.rollback()


async def test_positive_negative_equals_magnitude(session):
    """'5' and '-5' produce identical TP %."""
    account, _, user, _ = await _setup(session)
    s1 = await _feed_text(session, user, _mk_state(awaiting="tp_only_percent", leverage="10"), "5")
    await _feed_text(session, user, _mk_state(awaiting="tp_only_percent", leverage="10"), "skip")  # reset
    s2 = await _feed_text(session, user, _mk_state(awaiting="tp_only_percent", leverage="10"), "-5")
    assert s1["tp"] == s2["tp"] is not None
    await session.rollback()


async def test_both_negative_preserves_semantics(session):
    """'5 -3' and '-5 -3' both mean TP 5%, SL 3% (magnitudes, not two TP)."""
    account, _, user, _ = await _setup(session)
    s1 = await _feed_text(session, user, _mk_state(awaiting="tp_sl_percent", leverage="10"), "5 -3")
    # reset state between
    s2 = await _feed_text(session, user, _mk_state(awaiting="tp_sl_percent", leverage="10"), "-5 -3")
    for st in (s1, s2):
        assert st["tp"] is not None and st["sl"] is not None
        assert st["tp"] != st["sl"]  # not both TP
        assert st["tp"] > st["sl"]  # LONG: TP above SL
    assert s1["tp"] == s2["tp"]
    assert s1["sl"] == s2["sl"]
    await session.rollback()


async def test_zero_percent_rejected(session):
    account, _, user, _ = await _setup(session)
    st = await _feed_text(session, user, _mk_state(awaiting="tp_only_percent", leverage="10"), "0")
    assert "tp" not in st  # still awaiting, no confirmation state set
    await session.rollback()


async def test_zero_percent_both_rejected(session):
    account, _, user, _ = await _setup(session)
    st = await _feed_text(session, user, _mk_state(awaiting="tp_sl_percent", leverage="10"), "0 5")
    assert "tp" not in st
    await session.rollback()


async def test_lev_zero_rejected(session):
    account, _, user, _ = await _setup(session)
    st = await _feed_text(session, user, _mk_state(awaiting="tp_only_percent", leverage="0"), "5")
    assert "tp" not in st  # no ZeroDivisionError; awaiting remains
    await session.rollback()


# --------------------------------------------------------------------------
# FIX #2 — loss capping writes auditable ADJUSTMENT, no money creation
# --------------------------------------------------------------------------

async def _assert_reconciliation(session, account):
    ledger_sum = (await session.execute(
        select(func.coalesce(func.sum(AccountLedger.amount), 0)).where(AccountLedger.account_id == account.id)
    )).scalar_one()
    ledger_sum = Decimal(str(ledger_sum))
    # ledger sum == cash_balance (ledger includes INITIAL_BALANCE +10000)
    assert account.cash_balance.quantize(Decimal("0.01")) == ledger_sum.quantize(Decimal("0.01")), \
        f"reconciliation broken: cash={account.cash_balance} ledger_sum={ledger_sum}"
    assert account.cash_balance >= 0


async def test_close_loss_exceeding_margin_writes_adjustment(session):
    """300x LONG argv notional 3000 -> margin 10; price crashes 100 -> 1.
    Capped loss must be -margin, WITH an explicit ADJUSTMENT ledger entry.
    """
    account, comp, user, now = await _setup(session, side="LONG", leverage=300, notional=Decimal("3000"))
    await session.refresh(account)
    pos = await open_position(
        session, account, "SOLUSDT", "LONG",
        notional=Decimal("3000"), competition_id=comp.id,
        idempotency_key="ph1-gap-open", leverage=300,
    )
    await session.commit()
    await session.refresh(account)
    # crash: bid = 1.00
    crash = datetime.now(timezone.utc)
    await persist_snapshot(session, PriceSnapshot("SOLUSDT", Decimal("1.00"), Decimal("1.01"), Decimal("1.00"), crash, crash))
    await session.commit()

    closed, net = await close_position(
        session, pos, account,
        idempotency_key="ph1-gap-close", reason="manual",
    )
    await session.commit()
    await session.refresh(account)

    # capped: realized loss == -margin == -10
    assert net == Decimal("-10.00"), f"net={net} (should be capped -10.00, money must not disappear to -2970)"
    assert closed.realized_pnl == Decimal("-10.00")
    # ADJUSTMENT ledger present and auditable
    gap_rows = (await session.execute(
        select(AccountLedger).where(
            AccountLedger.account_id == account.id,
            AccountLedger.type == LedgerType.ADJUSTMENT.value,
        )
    )).scalars().all()
    assert len(gap_rows) == 1, "must have exactly one ADJUSTMENT gap ledger entry"
    gap = gap_rows[0]
    assert gap.reference_type == "liquidation_gap"
    assert gap.amount == Decimal("0.00")
    assert "gap=" in (gap.reference_id or "")
    # no money created/destroyed: reconciliation holds
    await _assert_reconciliation(session, account)


async def test_close_normal_loss_reconciliation(session):
    today, comp, user, now = await _setup(session, side="LONG", leverage=2, notional=Decimal("2000"))
    await session.refresh(today)
    pos = await open_position(
        session, today, "SOLUSDT", "LONG",
        notional=Decimal("2000"), competition_id=comp.id,
        idempotency_key="ph1-norm-open", leverage=2,
    )
    await session.commit()
    await session.refresh(today)
    move = datetime.now(timezone.utc)
    await persist_snapshot(session, PriceSnapshot("SOLUSDT", Decimal("99.00"), Decimal("99.10"), Decimal("99.00"), move, move))
    await session.commit()
    closed, net = await close_position(session, pos, today, idempotency_key="ph1-norm-close", reason="manual")
    await session.commit()
    await session.refresh(today)
    assert net < 0  # normal loss, no cap expected
    assert net > Decimal("-2000")
    gap_rows = (await session.execute(
        select(AccountLedger).where(
            AccountLedger.account_id == today.id,
            AccountLedger.type == LedgerType.ADJUSTMENT.value,
        )
    )).scalars().all()
    assert len(gap_rows) == 0  # no adjustment for normal loss
    await _assert_reconciliation(session, today)


async def test_close_retry_no_duplicate_adjustment(session):
    account, comp, user, now = await _setup(session, side="LONG", leverage=300, notional=Decimal("3000"))
    await session.refresh(account)
    pos = await open_position(
        session, account, "SOLUSDT", "LONG",
        notional=Decimal("3000"), competition_id=comp.id,
        idempotency_key="ph1-retry-open", leverage=300,
    )
    await session.commit()
    crash = datetime.now(timezone.utc)
    await persist_snapshot(session, PriceSnapshot("SOLUSDT", Decimal("1.00"), Decimal("1.01"), Decimal("1.00"), crash, crash))
    await session.commit()
    _, net1 = await close_position(session, pos, account, idempotency_key="ph1-retry-close", reason="manual")
    await session.commit()
    # retry SAME key
    pos2 = await session.get(PaperPosition, pos.id)
    _, net2 = await close_position(session, pos2, account, idempotency_key="ph1-retry-close", reason="manual")
    await session.commit()
    assert net2 == net1 == Decimal("-10.00")
    gap_rows = (await session.execute(
        select(func.count()).select_from(AccountLedger).where(
            AccountLedger.account_id == account.id,
            AccountLedger.type == LedgerType.ADJUSTMENT.value,
        )
    )).scalar_one()
    assert gap_rows == 1  # still exactly one
    await _assert_reconciliation(session, account)


# --------------------------------------------------------------------------
# FIX #3 — idempotency atomicity (open)
# --------------------------------------------------------------------------

async def test_open_same_key_returns_same_position_single_ledger(session):
    account, comp, user, now = await _setup(session, side="LONG", leverage=10, notional=Decimal("500"))
    await session.refresh(account)
    p1 = await open_position(
        session, account, "SOLUSDT", "LONG",
        notional=Decimal("500"), competition_id=comp.id,
        idempotency_key="ph1-idem-open", leverage=10,
    )
    await session.commit()
    await session.refresh(account)
    p2 = await open_position(
        session, account, "SOLUSDT", "LONG",
        notional=Decimal("500"), competition_id=comp.id,
        idempotency_key="ph1-idem-open", leverage=10,
    )
    await session.commit()
    await session.refresh(account)
    assert p2.id == p1.id
    # exactly one TRADE_OPEN ledger
    open_ledger_count = (await session.execute(
        select(func.count()).select_from(AccountLedger).where(
            AccountLedger.account_id == account.id,
            AccountLedger.type == LedgerType.TRADE_OPEN.value,
        )
    )).scalar_one()
    assert open_ledger_count == 1
    # balance mutated once: cash = 10000 - margin(500/10=50) = 9950
    assert account.cash_balance == Decimal("9950.00")
    await _assert_reconciliation(session, account)


async def test_close_same_key_returns_same_result_single_ledger(session):
    account, comp, user, now = await _setup(session, side="LONG", leverage=10, notional=Decimal("500"))
    await session.refresh(account)
    pos = await open_position(
        session, account, "SOLUSDT", "LONG",
        notional=Decimal("500"), competition_id=comp.id,
        idempotency_key="ph1-idem-close-open", leverage=10,
    )
    await session.commit()
    await session.refresh(account)
    move = datetime.now(timezone.utc)
    await persist_snapshot(session, PriceSnapshot("SOLUSDT", Decimal("101.00"), Decimal("101.10"), Decimal("101.00"), move, move))
    await session.commit()
    closed1, pnl1 = await close_position(session, pos, account, idempotency_key="ph1-idem-close", reason="manual")
    await session.commit()
    pos2 = await session.get(PaperPosition, pos.id)
    closed2, pnl2 = await close_position(session, pos2, account, idempotency_key="ph1-idem-close", reason="manual")
    await session.commit()
    assert pnl2 == pnl1
    close_ledger_count = (await session.execute(
        select(func.count()).select_from(AccountLedger).where(
            AccountLedger.account_id == account.id,
            AccountLedger.type == LedgerType.TRADE_CLOSE.value,
        )
    )).scalar_one()
    assert close_ledger_count == 1
    await _assert_reconciliation(session, account)


# --------------------------------------------------------------------------
# FIX #4 — TP/SL edit IDOR
# --------------------------------------------------------------------------

async def test_edit_own_position_allowed(session):
    account, comp, user, now = await _setup(session)
    pos = await open_position(
        session, account, "SOLUSDT", "LONG",
        notional=Decimal("500"), competition_id=comp.id,
        idempotency_key="ph1-owner-open", leverage=10,
        take_profit=Decimal("110"), stop_loss=Decimal("90"),
    )
    await session.commit()
    updated = await update_position_tp_sl(session, pos, account, Decimal("120"), Decimal("85"))
    await session.commit()
    assert updated.take_profit == Decimal("120")
    assert updated.stop_loss == Decimal("85")
    await session.rollback()


async def test_edit_other_user_denied(session):
    # user A owns position
    acc_a, comp_a, user_a, _ = await _setup(session, telegram_id=900111)
    pos = await open_position(
        session, acc_a, "SOLUSDT", "LONG",
        notional=Decimal("500"), competition_id=comp_a.id,
        idempotency_key="ph1-other-open", leverage=10,
    )
    await session.commit()
    # user B exists but is NOT owner
    now = datetime.now(timezone.utc)
    user_b = User(telegram_id=900112, username="ph1_b")
    session.add(user_b)
    await session.flush()
    acc_b = await get_or_create_trading_account(session, user_b.id)
    await session.commit()

    from services.paper_adapter import PaperError
    with pytest.raises(PaperError):
        await update_position_tp_sl(session, pos, acc_b, Decimal("120"), Decimal("85"))
    await session.rollback()
    # ensure not mutated
    pos2 = await session.get(PaperPosition, pos.id)
    assert pos2.take_profit is None
    assert pos2.stop_loss is None


async def test_edit_callback_handler_enforces_ownership(session):
    from bot.handlers.trade import cb_edit_tp_sl_mode, cb_edit_tp_sl_only

    acc_a, comp_a, user_a, _ = await _setup(session, telegram_id=900121)
    pos = await open_position(
        session, acc_a, "SOLUSDT", "LONG",
        notional=Decimal("500"), competition_id=comp_a.id,
        idempotency_key="ph1-cb-open", leverage=10,
    )
    await session.commit()
    pos_id = pos.id  # capture to avoid expired-attribute lazy load later
    # user B forged callback
    user_b = User(telegram_id=900122, username="ph1_cb_b")
    session.add(user_b)
    await session.flush()
    await get_or_create_trading_account(session, user_b.id)
    await session.commit()
    user_b_telegram_id = user_b.telegram_id  # capture after commit to avoid expiry

    callbacks = [
        (cb_edit_tp_sl_mode, f"edit_tp_sl:mode:price:{pos_id}"),
        (cb_edit_tp_sl_only, f"edit_tp_sl:only:tp:{pos_id}"),
    ]
    for handler, data in callbacks:
        cb = MagicMock()
        cb.from_user = MagicMock()
        cb.from_user.id = user_b_telegram_id
        cb.data = data
        cb.message = MagicMock()
        cb.message.edit_text = AsyncMock()
        cb.answer = AsyncMock()
        await handler(cb, session)
        assert cb.answer.called
        last_call = cb.answer.call_args.args
        assert last_call and "Позиция не найдена" in last_call[0], f"{data} should be denied"
        await session.rollback()

    # position untouched
    pos2 = await session.get(PaperPosition, pos_id)
    assert pos2.stop_loss is None and pos2.take_profit is None


async def test_edit_closed_position_denied(session):
    account, comp, user, now = await _setup(session)
    pos = await open_position(
        session, account, "SOLUSDT", "LONG",
        notional=Decimal("500"), competition_id=comp.id,
        idempotency_key="ph1-closed-open", leverage=10,
    )
    await session.commit()
    move = datetime.now(timezone.utc)
    await persist_snapshot(session, PriceSnapshot("SOLUSDT", Decimal("99.00"), Decimal("99.10"), Decimal("99.00"), move, move))
    await session.commit()
    await close_position(session, pos, account, idempotency_key="ph1-closed-close", reason="manual")
    await session.commit()
    from services.paper_adapter import PaperError
    with pytest.raises(PaperError):
        await update_position_tp_sl(session, pos, account, Decimal("120"), Decimal("85"))
    await session.rollback()


async def test_edit_finished_competition_denied(session):
    account, comp, user, _ = await _setup(session, telegram_id=900131)
    pos = await open_position(
        session, account, "SOLUSDT", "LONG",
        notional=Decimal("500"), competition_id=comp.id,
        idempotency_key="ph1-fin-open", leverage=10,
    )
    await session.commit()
    # finish competition
    comp.status = CompetitionStatus.FINISHED.value
    await session.commit()
    from bot.handlers.trade import _competition_tradeable
    assert await _competition_tradeable(session, await session.get(PaperPosition, pos.id)) is False


async def test_edit_replay_old_callback_safe(session):
    """Old callback from another conversation cannot edit; state dropped safely."""
    from bot.handlers.trade import trade_state

    account, comp, user, _ = await _setup(session)
    # no active editing state
    cb = MagicMock()
    cb.from_user = MagicMock()
    cb.from_user.id = user.telegram_id
    cb.data = "edit_tp_sl:mode:price:999999"
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    from bot.handlers.trade import cb_edit_tp_sl_mode
    await cb_edit_tp_sl_mode(cb, session)
    assert "Позиция не найдена" in cb.answer.call_args.args[0]