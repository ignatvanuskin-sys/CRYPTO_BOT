from __future__ import annotations
import enum
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import String, Text, Integer, Numeric, DateTime, ForeignKey, CheckConstraint, Index, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from db.base import Base

def utcnow():
    return datetime.now(timezone.utc)

class CompetitionStatus(str, enum.Enum):
    UPCOMING = "UPCOMING"
    ACTIVE = "ACTIVE"
    FINISHED = "FINISHED"
    CANCELLED = "CANCELLED"

class ExecutionReason(str, enum.Enum):
    OPEN = "OPEN"
    MANUAL_CLOSE = "MANUAL_CLOSE"
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    LIQUIDATION = "LIQUIDATION"

class Competition(Base):
    __tablename__ = "competitions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(SAEnum(CompetitionStatus, name="competition_status"), default=CompetitionStatus.UPCOMING.value, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    initial_balance: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("10000"))
    prize_pool: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    ranking_metric: Mapped[str] = mapped_column(Text, default="ROI", nullable=False)
    price_source: Mapped[str] = mapped_column(Text, default="BINGX", nullable=False)
    market_type: Mapped[str] = mapped_column(Text, default="USD_M_PERPETUAL", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

class CompetitionParticipant(Base):
    __tablename__ = "competition_participants"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    competition_id: Mapped[int] = mapped_column(Integer, ForeignKey("competitions.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    starting_equity: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    current_equity: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    roi: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False, default=Decimal("0"))
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint("competition_id", "user_id", name="uq_competition_user"),
        Index("ix_cp_competition", "competition_id"),
        Index("ix_cp_user", "user_id"),
    )

class Execution(Base):
    __tablename__ = "executions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(Integer, ForeignKey("paper_positions.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    competition_id: Mapped[int] = mapped_column(Integer, ForeignKey("competitions.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)  # LONG/SHORT
    price_source: Mapped[str] = mapped_column(Text, default="BINGX", nullable=False)
    market_type: Mapped[str] = mapped_column(Text, default="USD_M_PERPETUAL", nullable=False)
    bid_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    ask_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 12), nullable=True)
    execution_price: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    notional: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    market_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    execution_reason: Mapped[str] = mapped_column(SAEnum(ExecutionReason, name="execution_reason"), nullable=False)
    __table_args__ = (
        Index("ix_exec_position", "position_id"),
        Index("ix_exec_user_comp", "user_id", "competition_id"),
    )

class LeaderboardSnapshot(Base):
    __tablename__ = "competition_leaderboard_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    competition_id: Mapped[int] = mapped_column(Integer, ForeignKey("competitions.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    equity: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    roi: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint("competition_id", "user_id", name="uq_snapshot_comp_user"),
        Index("ix_snapshot_comp_rank", "competition_id", "rank"),
    )


class CompetitionPrize(Base):
    __tablename__ = "competition_prizes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    competition_id: Mapped[int] = mapped_column(Integer, ForeignKey("competitions.id"), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="ASSIGNED")
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("competition_id", "rank", name="uq_competition_prize_rank"),
        Index("ix_competition_prize_competition", "competition_id"),
    )
