"""«Повторить сделку»: параметры восстанавливаются на сервере, а не берутся из callback_data.

В кнопке едет только id позиции, поэтому подменённый `retry:{чужой_id}` — это попытка
прочитать чужую сделку. Тот же IDOR-guard, что и на закрытии/TP-SL, должен её отклонить.
"""

from decimal import Decimal

import pytest
import pytest_asyncio

from bot.handlers import trade as trade_handlers
from db.models import User
from db.paper_models import (
    Instrument,
    InstrumentStatus,
    PaperPosition,
    PositionSide,
    PositionStatus,
    TradingAccount,
)

OWNER_TG_ID = 5001
ATTACKER_TG_ID = 5002


class _FakeMessage:
    def __init__(self):
        self.edits: list[str] = []
        self.answers: list[str] = []

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)

    async def answer(self, text, **kwargs):
        self.answers.append(text)


class _FakeCallback:
    def __init__(self, data: str, telegram_id: int):
        self.data = data
        self.from_user = type("_U", (), {"id": telegram_id})()
        self.message = _FakeMessage()
        self.answers: list[str | None] = []
        self.alerts: list[str | None] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)
        if show_alert:
            self.alerts.append(text)


@pytest.fixture(autouse=True)
def clean_trade_state():
    trade_handlers.trade_state.clear()
    yield
    trade_handlers.trade_state.clear()


async def _account_for(session, telegram_id: int) -> TradingAccount:
    user = User(telegram_id=telegram_id, username=f"u{telegram_id}")
    session.add(user)
    await session.flush()
    account = TradingAccount(
        user_id=user.id,
        cash_balance=Decimal("10000"),
        equity=Decimal("10000"),
        available_margin=Decimal("10000"),
        initial_balance=Decimal("10000"),
    )
    session.add(account)
    await session.flush()
    return account


@pytest_asyncio.fixture
async def closed_position(session):
    """Закрытая позиция владельца + пустой аккаунт постороннего юзера."""
    session.add(
        Instrument(
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            status=InstrumentStatus.active.value,
            max_leverage=300,
        )
    )
    owner_account = await _account_for(session, OWNER_TG_ID)
    await _account_for(session, ATTACKER_TG_ID)
    position = PaperPosition(
        account_id=owner_account.id,
        symbol="BTCUSDT",
        side=PositionSide.SHORT.value,
        status=PositionStatus.CLOSED.value,
        quantity=Decimal("0.02"),
        entry_price=Decimal("60000"),
        current_price=Decimal("59000"),
        notional=Decimal("1200.00"),
        leverage=Decimal("50"),
        take_profit=Decimal("58000"),
        stop_loss=Decimal("61000"),
        realized_pnl=Decimal("20.00"),
    )
    session.add(position)
    await session.commit()
    return position


@pytest.mark.asyncio
async def test_foreign_position_is_rejected(session, closed_position):
    callback = _FakeCallback(f"retry:{closed_position.id}", ATTACKER_TG_ID)

    await trade_handlers.cb_retry_trade(callback, session)

    assert callback.alerts == ["Сделка не найдена"]
    # Ни состояния сделки, ни отрисованного экрана подтверждения
    assert ATTACKER_TG_ID not in trade_handlers.trade_state
    assert callback.message.edits == []
    assert callback.message.answers == []


@pytest.mark.asyncio
async def test_unknown_position_is_rejected(session, closed_position):
    callback = _FakeCallback("retry:999999", OWNER_TG_ID)

    await trade_handlers.cb_retry_trade(callback, session)

    assert callback.alerts == ["Сделка не найдена"]
    assert OWNER_TG_ID not in trade_handlers.trade_state


@pytest.mark.asyncio
async def test_owner_gets_parameters_restored_from_db(session, closed_position):
    callback = _FakeCallback(f"retry:{closed_position.id}", OWNER_TG_ID)

    await trade_handlers.cb_retry_trade(callback, session)

    state = trade_handlers.trade_state[OWNER_TG_ID]
    assert state["symbol"] == "BTCUSDT"
    assert state["side"] == "SHORT"
    assert Decimal(state["leverage"]) == Decimal("50")
    # Бюджет = маржа исходной сделки: notional 1200 / плечо 50
    assert Decimal(state["budget"]) == Decimal("24.00")
    # TP/SL не переносим — абсолютные цены закрытой сделки уже неактуальны
    assert state["tp"] is None and state["sl"] is None
    assert callback.message.edits, "должен открыться экран подтверждения"
    assert "ПОДТВЕРЖДЕНИЕ СДЕЛКИ" in callback.message.edits[0]


@pytest.mark.asyncio
async def test_delisted_pair_is_rejected(session, closed_position):
    instrument = await session.get(Instrument, "BTCUSDT")
    instrument.status = InstrumentStatus.delisted.value
    await session.commit()

    callback = _FakeCallback(f"retry:{closed_position.id}", OWNER_TG_ID)
    await trade_handlers.cb_retry_trade(callback, session)

    assert callback.alerts == ["Пара сейчас недоступна"]
    assert OWNER_TG_ID not in trade_handlers.trade_state
