from __future__ import annotations
import enum
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import BigInteger, String, Text, Boolean, DateTime, Integer, Numeric, ForeignKey, CheckConstraint, Index, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from db.base import Base

def utcnow():
    return datetime.now(timezone.utc)

class TradingAccountStatus(str, enum.Enum):
    active = "active"
    suspended = "suspended"

class InstrumentStatus(str, enum.Enum):
    active = "active"
    delisted = "delisted"

class LedgerType(str, enum.Enum):
    INITIAL_BALANCE = "INITIAL_BALANCE"
    TRADE_OPEN = "TRADE_OPEN"
    TRADE_CLOSE = "TRADE_CLOSE"
    FEE = "FEE"
    ADJUSTMENT = "ADJUSTMENT"

class OrderSide(str, enum.Enum):
    LONG = "LONG"
    SHORT = "SHORT"

class OrderType(str, enum.Enum):
    MARKET = "MARKET"

class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    REJECTED = "REJECTED"

class PositionSide(str, enum.Enum):
    LONG = "LONG"
    SHORT = "SHORT"

class PositionStatus(str, enum.Enum):
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"

class TradingAccount(Base):
    __tablename__ = "trading_accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="USD", nullable=False)
    initial_balance: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("10000"))
    cash_balance: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    equity: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    margin_used: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    available_margin: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    total_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

class Instrument(Base):
    __tablename__ = "instruments"
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)  # e.g. BTCUSDT
    base_asset: Mapped[str] = mapped_column(Text, nullable=False)
    quote_asset: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(SAEnum(InstrumentStatus, name="instrument_status"), default=InstrumentStatus.active.value, nullable=False)
    price_precision: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    quantity_precision: Mapped[int] = mapped_column(Integer, default=6, nullable=False)
    min_quantity: Mapped[Decimal] = mapped_column(Numeric(30, 12), default=Decimal("0.000001"), nullable=False)
    max_quantity: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

class AccountLedger(Base):
    __tablename__ = "account_ledger"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("trading_accounts.id"), nullable=False)
    type: Mapped[str] = mapped_column(SAEnum(LedgerType, name="ledger_type"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    balance_after: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    reference_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    reference_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        CheckConstraint("balance_after >= 0", name="ck_ledger_balance_after_non_negative"),
        Index("ix_ledger_account", "account_id"),
        Index("ix_ledger_idempotency", "idempotency_key", unique=True),
    )

class PaperOrder(Base):
    __tablename__ = "paper_orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("trading_accounts.id"), nullable=False)
    position_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("paper_positions.id"), nullable=True)
    symbol: Mapped[str] = mapped_column(String(20), ForeignKey("instruments.symbol"), nullable=False)
    side: Mapped[str] = mapped_column(SAEnum(OrderSide, name="paper_order_side"), nullable=False)
    order_type: Mapped[str] = mapped_column(SAEnum(OrderType, name="paper_order_type"), default=OrderType.MARKET.value, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    requested_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    executed_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    status: Mapped[str] = mapped_column(SAEnum(OrderStatus, name="paper_order_status"), default=OrderStatus.PENDING.value, nullable=False)
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class PaperPosition(Base):
    __tablename__ = "paper_positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("trading_accounts.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), ForeignKey("instruments.symbol"), nullable=False)
    side: Mapped[str] = mapped_column(SAEnum(PositionSide, name="paper_position_side"), nullable=False)
    status: Mapped[str] = mapped_column(SAEnum(PositionStatus, name="paper_position_status"), default=PositionStatus.OPEN.value, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    current_price: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    notional: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    take_profit: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    fee_open: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    fee_close: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_paper_position_qty_positive"),
        Index("ix_paper_positions_account_status", "account_id", "status"),
        Index("ix_paper_positions_symbol", "symbol"),
    )

class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    entity_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
