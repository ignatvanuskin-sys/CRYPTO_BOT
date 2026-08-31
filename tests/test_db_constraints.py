"""db/paper_models.py + db/competition_models.py — constraints и enums."""
from decimal import Decimal

import pytest
from sqlalchemy import select

from db.models import User
from db.paper_models import PaperPosition, PositionStatus, AccountLedger, LedgerType
from db.competition_models import Execution, ExecutionReason


class TestLedgerConstraints:
    @pytest.mark.asyncio
    async def test_negative_balance_after_rejected(self, session):
        """CHECK balance_after >= 0 — физически невозможно создать отрицательный баланс."""
        from db.paper_models import TradingAccount
        from services.trading_account import get_or_create_trading_account
        from services.accounts import get_or_create_user

        u = await get_or_create_user(session, 77777, "neg_test")
        acc = await get_or_create_trading_account(session, u.id)
        # Попытка вставить ledger с отрицательным balance_after
        bad = AccountLedger(
            account_id=acc.id, type=LedgerType.TRADE_OPEN.value,
            amount=Decimal("-50000"), balance_after=Decimal("-50000"),
            reference_type="test", reference_id="neg",
            idempotency_key="neg-test-unique",
        )
        session.add(bad)
        from sqlalchemy.exc import IntegrityError
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    @pytest.mark.asyncio
    async def test_idempotency_key_unique(self, session):
        from db.paper_models import TradingAccount
        from services.trading_account import get_or_create_trading_account
        from services.accounts import get_or_create_user

        u = await get_or_create_user(session, 77778, "idem_test")
        acc = await get_or_create_trading_account(session, u.id)
        l1 = AccountLedger(account_id=acc.id, type=LedgerType.TRADE_OPEN.value,
                           amount=Decimal("-100"), balance_after=Decimal("9900"),
                           idempotency_key="idem-constraint-test")
        session.add(l1)
        await session.commit()

        l2 = AccountLedger(account_id=acc.id, type=LedgerType.TRADE_OPEN.value,
                           amount=Decimal("-200"), balance_after=Decimal("9800"),
                           idempotency_key="idem-constraint-test")  # SAME key
        session.add(l2)
        from sqlalchemy.exc import IntegrityError
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


class TestExecutionReasons:
    def test_all_reasons_exist(self):
        for r in ["OPEN", "MANUAL_CLOSE", "TAKE_PROFIT", "STOP_LOSS", "LIQUIDATION"]:
            assert hasattr(ExecutionReason, r), f"missing {r}"

    def test_liquidation_value(self):
        assert ExecutionReason.LIQUIDATION.value == "LIQUIDATION"