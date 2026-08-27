from __future__ import annotations
import enum
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import (
    BigInteger, String, Text, Boolean, DateTime, Integer, Numeric, Enum as SAEnum,
    ForeignKey, UniqueConstraint, CheckConstraint, Index
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from db.base import Base

def utcnow():
    return datetime.now(timezone.utc)

class WeekStatus(str, enum.Enum):
    active = "active"
    closing = "closing"
    closed = "closed"

class AssetStatus(str, enum.Enum):
    active = "active"
    delisted = "delisted"

class TransactionType(str, enum.Enum):
    WEEKLY_GRANT = "WEEKLY_GRANT"
    TRADE_BUY = "TRADE_BUY"
    TRADE_SELL = "TRADE_SELL"
    FORCED_CLOSE = "FORCED_CLOSE"
    ADJUSTMENT = "ADJUSTMENT"

class OrderSide(str, enum.Enum):
    buy = "buy"
    sell = "sell"

class OrderStatus(str, enum.Enum):
    pending = "pending"
    filled = "filled"
    rejected = "rejected"

class PayoutStatus(str, enum.Enum):
    pending = "pending"
    verified = "verified"
    paid = "paid"
    rejected = "rejected"

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone_number: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True)
    phone_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rules_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ban_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

class Week(Base):
    __tablename__ = "weeks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    week_number: Mapped[int] = mapped_column(Integer, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(SAEnum(WeekStatus, name="week_status"), default=WeekStatus.active.value, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class Asset(Base):
    __tablename__ = "assets"
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    base_asset: Mapped[str] = mapped_column(Text, nullable=False)
    quote_asset: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(SAEnum(AssetStatus, name="asset_status"), default=AssetStatus.active.value, nullable=False)
    is_quote_eligible: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_24h_quote_volume: Mapped[Decimal | None] = mapped_column(Numeric(30, 10), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class Transaction(Base):
    __tablename__ = "transactions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    week_id: Mapped[int] = mapped_column(Integer, ForeignKey("weeks.id"), nullable=False)
    type: Mapped[str] = mapped_column(SAEnum(TransactionType, name="transaction_type"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    balance_after: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    ref_order_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("orders.id"), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint("balance_after >= 0", name="ck_balance_after_non_negative"),
        # partial unique for WEEKLY_GRANT handled via app logic + index; PG partial index via alembic
        Index("ix_transactions_user_week", "user_id", "week_id"),
        Index("ix_transactions_idempotency", "idempotency_key", unique=True),
    )

class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    week_id: Mapped[int] = mapped_column(Integer, ForeignKey("weeks.id"), nullable=False)
    asset_symbol: Mapped[str] = mapped_column(String(32), ForeignKey("assets.symbol"), nullable=False)
    side: Mapped[str] = mapped_column(SAEnum(OrderSide, name="order_side"), nullable=False)
    notional_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    qty: Mapped[Decimal | None] = mapped_column(Numeric(30, 10), nullable=True)
    status: Mapped[str] = mapped_column(SAEnum(OrderStatus, name="order_status"), default=OrderStatus.pending.value, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    executed_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    price_source_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

class Position(Base):
    __tablename__ = "positions"
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), primary_key=True)
    week_id: Mapped[int] = mapped_column(Integer, ForeignKey("weeks.id"), primary_key=True)
    asset_symbol: Mapped[str] = mapped_column(String(32), ForeignKey("assets.symbol"), primary_key=True)
    qty: Mapped[Decimal] = mapped_column(Numeric(30, 10), nullable=False, default=Decimal("0"))
    avg_entry_price: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False, default=Decimal("0"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint("qty >= 0", name="ck_position_qty_non_negative"),
    )

class LeaderboardSnapshot(Base):
    __tablename__ = "leaderboard_snapshots"
    week_id: Mapped[int] = mapped_column(Integer, ForeignKey("weeks.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), primary_key=True)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    cash_balance: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    positions_value: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    total_equity: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

class Prize(Base):
    __tablename__ = "prizes"
    week_id: Mapped[int] = mapped_column(Integer, ForeignKey("weeks.id"), primary_key=True)
    rank: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payout_status: Mapped[str] = mapped_column(SAEnum(PayoutStatus, name="payout_status"), default=PayoutStatus.pending.value, nullable=False)
    verified_by_admin_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
