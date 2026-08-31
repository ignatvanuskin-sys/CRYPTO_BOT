"""bot/middlewares/throttling.py — rate limiting per user."""
import time
from unittest.mock import AsyncMock, MagicMock, create_autospec
from aiogram.types import Message, CallbackQuery

import pytest

from bot.middlewares.throttling import ThrottlingMiddleware


def _msg(uid, text="hello"):
    m = MagicMock()
    m.from_user = MagicMock()
    m.from_user.id = uid
    m.text = text
    m.answer = AsyncMock()
    return m


def _cb(uid):
    c = MagicMock()
    c.from_user = MagicMock()
    c.from_user.id = uid
    c.answer = AsyncMock()
    del c.text  # CallbackQuery has no .text — duck typing in middleware
    return c


@pytest.fixture
def mw():
    return ThrottlingMiddleware(message_rate=0.1, callback_rate=0.05)


@pytest.fixture
def handler():
    state = {"count": 0}
    async def h(event, data):
        state["count"] += 1
        return "ok"
    h.state = state
    return h


@pytest.mark.asyncio
class TestThrottling:
    async def test_first_message_passes(self, mw, handler):
        assert await mw(handler, _msg(1), {}) == "ok"

    async def test_rapid_message_blocked(self, mw, handler):
        msg = _msg(1, "/start")
        await mw(handler, msg, {})
        result = await mw(handler, msg, {})
        assert result is None
        assert handler.state["count"] == 1

    async def test_rapid_callback_blocked(self, mw, handler):
        cb = _cb(1)
        await mw(handler, cb, {})
        result = await mw(handler, cb, {})
        assert result is None

    async def test_different_users_pass(self, mw, handler):
        await mw(handler, _msg(1), {})
        await mw(handler, _msg(2), {})
        assert handler.state["count"] == 2

    async def test_command_alert_shown(self, mw, handler):
        msg = _msg(1, "/start")
        await mw(handler, msg, {})
        msg2 = _msg(1, "/top")
        await mw(handler, msg2, {})
        msg2.answer.assert_called_once()

    async def test_callback_answer_on_block(self, mw, handler):
        """Callbacks on the same user as a recent message use a DIFFERENT user ID
        to isolate the message-throttle from the callback-throttle."""
        cb = _cb(10)
        cb.answer = AsyncMock()
        await mw(handler, cb, {})
        cb2 = _cb(10)
        cb2.answer = AsyncMock()
        await mw(handler, cb2, {})
        cb2.answer.assert_called_once_with("Слишком быстро", show_alert=False)

    async def test_prune_removes_stale(self, mw, handler):
        cb = _cb(1)
        await mw(handler, cb, {})
        mw._last_callback[1] = time.monotonic() - 7200
        mw._last_prune = time.monotonic() - 700
        mw._prune_if_needed()
        assert 1 not in mw._last_callback

    def test_register(self, mw):
        registered = {"msg": False, "cb": False}
        class DP:
            class message:
                @staticmethod
                def middleware(m): registered["msg"] = True
            class callback_query:
                @staticmethod
                def middleware(m): registered["cb"] = True
        mw.register(DP)
        assert registered["msg"] and registered["cb"]