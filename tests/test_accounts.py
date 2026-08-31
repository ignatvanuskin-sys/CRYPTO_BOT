"""services/accounts.py + services/trading_account.py — юзеры, верификация, счёт."""
from decimal import Decimal
from datetime import datetime, timezone

import pytest

from db.models import User
from db.paper_models import TradingAccount, AccountLedger, PaperPosition, PositionStatus
from services.accounts import get_or_create_user, verify_phone, ensure_can_trade
from services.trading_account import get_or_create_trading_account, refresh_account_stats

pytestmark = pytest.mark.asyncio


class TestGetOrCreateUser:
    async def test_creates_new(self, session):
        user = await get_or_create_user(session, 12345, "alice")
        await session.commit()
        assert user.telegram_id == 12345
        assert user.username == "alice"

    async def test_idempotent(self, session):
        u1 = await get_or_create_user(session, 12345, "alice")
        u2 = await get_or_create_user(session, 12345, "alice")
        assert u1.id == u2.id

    async def test_updates_username(self, session):
        u1 = await get_or_create_user(session, 12345, "old_name")
        u2 = await get_or_create_user(session, 12345, "new_name")
        assert u2.username == "new_name"

    async def test_no_username(self, session):
        u = await get_or_create_user(session, 99999, None)
        assert u.username is None


class TestVerifyPhone:
    async def test_sets_phone_and_verified(self, session):
        u = await get_or_create_user(session, 12345, "alice")
        await verify_phone(session, u, "+79001234567")
        assert u.phone_number == "+79001234567"
        assert u.phone_verified_at is not None


class TestEnsureCanTrade:
    async def test_ok(self, session):
        u = await get_or_create_user(session, 12345, "alice")
        await verify_phone(session, u, "+79001234567")
        ensure_can_trade(u)  # no exception

    async def test_banned_raises(self, session):
        u = await get_or_create_user(session, 12345, "alice")
        await verify_phone(session, u, "+79001234567")
        u.is_banned = True
        with pytest.raises(PermissionError, match="[Bb]anned"):
            ensure_can_trade(u)

    async def test_no_phone_raises(self, session):
        u = await get_or_create_user(session, 12345, "alice")
        with pytest.raises(PermissionError, match="[Pp]hone"):
            ensure_can_trade(u)


class TestGetOrCreateTradingAccount:
    async def test_creates_with_10000(self, session):
        u = await get_or_create_user(session, 12345, "alice")
        acc = await get_or_create_trading_account(session, u.id)
        await session.commit()
        assert acc.initial_balance == Decimal("10000")
        assert acc.cash_balance == Decimal("10000")

    async def test_idempotent(self, session):
        u = await get_or_create_user(session, 12345, "alice")
        a1 = await get_or_create_trading_account(session, u.id)
        a2 = await get_or_create_trading_account(session, u.id)
        assert a1.id == a2.id

    async def test_initial_balance_ledger(self, session):
        u = await get_or_create_user(session, 12345, "alice")
        acc = await get_or_create_trading_account(session, u.id)
        await session.commit()
        rows = (await session.execute(
            __import__('sqlalchemy').select(AccountLedger).where(AccountLedger.account_id == acc.id)
        )).scalars().all()
        assert any(r.type == "INITIAL_BALANCE" and r.amount == Decimal("10000") for r in rows)