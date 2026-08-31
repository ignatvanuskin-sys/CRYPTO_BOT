"""Пуши о закрытии позиций: один цикл движка — одно сообщение на пользователя.

Резкое движение рынка ликвидирует несколько позиций за один тик tp_sl_engine.
Без батчинга юзер получал бы очередь отдельных пушей — проверяем, что вместо
этого уходит одна сводка, и что одиночное закрытие по-прежнему выглядит как раньше.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

import services.notifications as notifications
from db.models import User
from services.notifications import MAX_RETRY_BUTTONS, group_close_events_by_user


class _FakeBotSession:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class _FakeBot:
    """Заглушка aiogram.Bot — собирает отправленное вместо сети."""

    def __init__(self, token: str, **kwargs):
        self.token = token
        self.sent: list[dict] = []
        self.session = _FakeBotSession()

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})


@pytest.fixture
def fake_bots(monkeypatch):
    created: list[_FakeBot] = []

    def _factory(token: str, **kwargs):
        bot = _FakeBot(token, **kwargs)
        created.append(bot)
        return bot

    monkeypatch.setattr(notifications, "Bot", _factory)
    monkeypatch.setattr(notifications, "settings", SimpleNamespace(bot_token="123:TEST"))
    return created


@pytest_asyncio.fixture
async def users(sqlite_engine):
    """Двое реальных юзеров и один симулированный бот-участник."""
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as session:
        trader = User(telegram_id=1001, username="trader")
        other = User(telegram_id=1002, username="other")
        simulated = User(telegram_id=1003, username="sim", is_simulated=True)
        session.add_all([trader, other, simulated])
        await session.commit()
        return SimpleNamespace(trader=trader, other=other, simulated=simulated)


def _event(position_id: int, user_id: int, symbol: str, reason: str, pnl: str, side="LONG", leverage=50):
    return {
        "position_id": position_id,
        "user_id": user_id,
        "symbol": symbol,
        "side": side,
        "leverage": Decimal(str(leverage)),
        "pnl": Decimal(pnl),
        "reason": reason,
    }


def test_grouping_keeps_close_order_per_user():
    events = [
        _event(1, 7, "BTCUSDT", "LIQUIDATION", "-98.00"),
        _event(2, 8, "ETHUSDT", "TP", "+12.00"),
        _event(3, 7, "SOLUSDT", "SL", "-4.00"),
        _event(4, 0, "XRPUSDT", "TP", "+1.00"),  # без user_id — молча пропускаем
    ]
    grouped = group_close_events_by_user(events)
    assert list(grouped.keys()) == [7, 8]
    assert [e["symbol"] for e in grouped[7]] == ["BTCUSDT", "SOLUSDT"]


@pytest.mark.asyncio
async def test_batch_of_closes_becomes_one_message(sqlite_engine, users, fake_bots):
    events = [
        _event(11, users.trader.id, "BTCUSDT", "LIQUIDATION", "-98.00"),
        _event(12, users.trader.id, "ETHUSDT", "LIQUIDATION", "-45.50"),
        _event(13, users.trader.id, "SOLUSDT", "SL", "-10.00"),
        _event(14, users.trader.id, "XRPUSDT", "TP", "+7.25"),
        _event(15, users.other.id, "BTCUSDT", "TP", "+125.30"),
    ]
    await notifications.notify_positions_closed(sqlite_engine, events)

    sent = fake_bots[0].sent
    # 4 закрытия у первого юзера + 1 у второго = 2 сообщения, не 5
    assert len(sent) == 2
    by_chat = {msg["chat_id"]: msg for msg in sent}
    assert set(by_chat) == {1001, 1002}

    batch = by_chat[1001]["text"]
    assert "ЗАКРЫТО ПОЗИЦИЙ: 4" in batch
    for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"):
        assert symbol in batch
    assert "ликвидация" in batch and "стоп-лосс" in batch and "тейк-профит" in batch
    # Итог = -98.00 - 45.50 - 10.00 + 7.25
    assert "Итог: <b>$-146.25</b>" in batch

    # Кнопок повтора не больше лимита — иначе клавиатура нечитаема
    rows = by_chat[1001]["reply_markup"].inline_keyboard
    assert len(rows) == MAX_RETRY_BUTTONS
    assert [row[0].callback_data for row in rows] == ["retry:11", "retry:12", "retry:13"]
    assert all(row[0].text.startswith("Повторить ") for row in rows)

    assert fake_bots[0].session.closed


@pytest.mark.asyncio
async def test_single_close_keeps_detailed_card(sqlite_engine, users, fake_bots):
    await notifications.notify_positions_closed(
        sqlite_engine,
        [_event(21, users.trader.id, "BTCUSDT", "LIQUIDATION", "-98.00", leverage=300)],
    )
    sent = fake_bots[0].sent
    assert len(sent) == 1
    assert "ЛИКВИДАЦИЯ" in sent[0]["text"]
    assert "Позиция закрыта принудительно" in sent[0]["text"]
    assert "<b>$-98.00</b>" in sent[0]["text"]
    assert "x300" in sent[0]["text"]
    rows = sent[0]["reply_markup"].inline_keyboard
    assert len(rows) == 1
    assert rows[0][0].callback_data == "retry:21"
    assert rows[0][0].text == "Повторить сделку"


@pytest.mark.asyncio
async def test_profit_is_shown_with_plus(sqlite_engine, users, fake_bots):
    await notifications.notify_positions_closed(
        sqlite_engine, [_event(31, users.other.id, "ETHUSDT", "TP", "+125.30", side="SHORT")]
    )
    text = fake_bots[0].sent[0]["text"]
    assert "ТЕЙК-ПРОФИТ СРАБОТАЛ" in text
    assert "Позиция закрыта: <b>+$125.30</b>" in text


@pytest.mark.asyncio
async def test_simulated_users_get_nothing(sqlite_engine, users, fake_bots):
    await notifications.notify_positions_closed(
        sqlite_engine,
        [
            _event(41, users.simulated.id, "BTCUSDT", "TP", "+1.00"),
            _event(42, users.simulated.id, "ETHUSDT", "SL", "-1.00"),
        ],
    )
    assert fake_bots[0].sent == []


@pytest.mark.asyncio
async def test_send_failure_does_not_stop_other_users(sqlite_engine, users, fake_bots, monkeypatch):
    async def _explode(self, chat_id, text, **kwargs):
        if chat_id == 1001:
            raise RuntimeError("Telegram is down")
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})

    monkeypatch.setattr(_FakeBot, "send_message", _explode)
    await notifications.notify_positions_closed(
        sqlite_engine,
        [
            _event(51, users.trader.id, "BTCUSDT", "TP", "+1.00"),
            _event(52, users.other.id, "ETHUSDT", "TP", "+2.00"),
        ],
    )
    assert [msg["chat_id"] for msg in fake_bots[0].sent] == [1002]
    assert fake_bots[0].session.closed


@pytest.mark.asyncio
async def test_nothing_sent_without_events_or_token(sqlite_engine, users, fake_bots, monkeypatch):
    await notifications.notify_positions_closed(sqlite_engine, [])
    assert fake_bots == []

    monkeypatch.setattr(notifications, "settings", SimpleNamespace(bot_token=""))
    await notifications.notify_positions_closed(
        sqlite_engine, [_event(61, users.trader.id, "BTCUSDT", "TP", "+1.00")]
    )
    assert fake_bots == []
