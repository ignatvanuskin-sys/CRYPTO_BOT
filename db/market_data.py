from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MarketSnapshot(Base):
    """Latest authoritative BingX perpetual snapshot shared by all processes."""

    __tablename__ = "market_snapshots"

    symbol: Mapped[str] = mapped_column(String(40), primary_key=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="BINGX")
    market_type: Mapped[str] = mapped_column(String(16), nullable=False, default="PERPETUAL")
    bid: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    ask: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    last: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    exchange_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint("bid > 0", name="ck_market_snapshot_bid_positive"),
        CheckConstraint("ask > 0", name="ck_market_snapshot_ask_positive"),
        CheckConstraint("ask >= bid", name="ck_market_snapshot_ask_ge_bid"),
    )
