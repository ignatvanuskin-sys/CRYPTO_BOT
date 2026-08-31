"""bot/handlers/admin.py — все админ-команды: auth, guards, functionality."""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.handlers.admin import is_admin, admin_stats, admin_ban, admin_unban, admin_product_stats
from config import settings
from db.models import User
from db.paper_models import TradingAccount

pytestmark = pytest.mark.asyncio

ADMIN_ID = 111
NON_ADMIN_ID = 222


@pytest.fixture
def admin_env(monkeypatch):
    monkeypatch.setattr(settings, "admin_telegram_ids", str(ADMIN_ID))


def _msg(uid, text="/cmd"):
    m = MagicMock()
    m.from_user = MagicMock(); m.from_user.id = uid
    m.text = text
    m.answer = AsyncMock()
    return m


class TestIsAdmin:
    def test_admin(self, admin_env): assert is_admin(ADMIN_ID) is True
    def test_non_admin(self, admin_env): assert is_admin(NON_ADMIN_ID) is False
    def test_no_admins(self, monkeypatch):
        monkeypatch.setattr(settings, "admin_telegram_ids", "")
        assert is_admin(ADMIN_ID) is False


class TestAdminStats:
    async def test_admin_gets_stats(self, admin_env, session):
        msg = _msg(ADMIN_ID)
        await admin_stats(msg, session)
        assert msg.answer.called
        assert "Users" in msg.answer.call_args[0][0]

    async def test_non_admin_denied(self, admin_env, session):
        msg = _msg(NON_ADMIN_ID)
        await admin_stats(msg, session)
        assert msg.answer.call_args[0][0] == "Нет доступа"

    async def test_no_user_denied(self, admin_env, session):
        msg = _msg(ADMIN_ID)
        msg.from_user = None
        await admin_stats(msg, session)
        assert msg.answer.call_args[0][0] == "Нет доступа"


class TestAdminBan:
    async def test_ban_sets_flag(self, admin_env, session):
        from services.accounts import get_or_create_user
        target = await get_or_create_user(session, 999, "victim")
        await session.commit()
        msg = _msg(ADMIN_ID, f"/admin_ban 999 test reason")
        await admin_ban(msg, session)
        await session.refresh(target)
        assert target.is_banned is True

    async def test_non_admin_denied(self, admin_env, session):
        msg = _msg(NON_ADMIN_ID, "/admin_ban 999 x")
        await admin_ban(msg, session)
        assert msg.answer.call_args[0][0] == "Нет доступа"


class TestAdminUnban:
    async def test_unban_clears_flag(self, admin_env, session):
        from services.accounts import get_or_create_user
        target = await get_or_create_user(session, 999, "victim")
        target.is_banned = True
        await session.commit()
        msg = _msg(ADMIN_ID, f"/admin_unban 999")
        await admin_unban(msg, session)
        await session.refresh(target)
        assert target.is_banned is False