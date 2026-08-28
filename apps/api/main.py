from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apps.api.auth import parse_user_from_init_data, validate_init_data
from config import settings
from db.market_data import MarketSnapshot
from db.models import User
from db.paper_models import Instrument, PaperPosition, TradingAccount
from services.bingx_market_data import normalize_symbol
from services.metrics import increment, snapshot as metrics_snapshot
from services.competition import get_active_competition, join_competition
from services.paper_adapter import (
    InsufficientMargin,
    InvalidQuantity,
    InvalidSymbol,
    InvalidTP_SL,
    PaperError,
    close_position,
    open_position,
)
from services.trading_account import get_or_create_trading_account

logger = logging.getLogger(__name__)

if settings.require_postgres and not settings.database_is_postgres:
    raise RuntimeError("REQUIRE_POSTGRES=true but DATABASE_URL is not PostgreSQL")

app = FastAPI(title="Trade CryptoBot API", version="0.2")

_allowed_origins = [
    value.strip()
    for value in settings.webapp_url.split(",")
    if value.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins or ["*"],
    allow_credentials=bool(_allowed_origins),
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Telegram-Init-Data", "Idempotency-Key"],
)

engine = create_async_engine(settings.database_url_async, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session():
    async with SessionLocal() as session:
        yield session


async def _market_data_state(session) -> str:
    rows = await session.execute(
        select(MarketSnapshot).where(
            MarketSnapshot.symbol.in_(["BTCUSDT", "ETHUSDT", "SOLUSDT"])
        )
    )
    now = datetime.now(timezone.utc)
    fresh = {
        row.symbol
        for row in rows.scalars().all()
        if (now - row.exchange_timestamp).total_seconds() * 1000 <= settings.market_data_max_age_ms
    }
    return "ok" if {"BTCUSDT", "ETHUSDT", "SOLUSDT"}.issubset(fresh) else "no_data"


@app.get("/health")
async def health():
    """Liveness endpoint: stays useful even when DB/BingX are unavailable."""
    database = "ok"
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
            market_data = await _market_data_state(session)
    except Exception:
        database = "error"
        market_data = "no_data"
    return {
        "status": "ok" if database == "ok" else "degraded",
        "database": database,
        "market_data": market_data,
        "trading_mode": settings.trading_mode,
    }


@app.get("/ready")
async def ready():
    """Readiness endpoint for API dependencies; no exception trace is exposed."""
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
            market_data = await _market_data_state(session)
    except Exception:
        logger.exception("Readiness check failed")
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "database": "error", "market_data": "no_data"},
        )
    if market_data != "ok":
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "database": "ok", "market_data": market_data},
        )
    return {"status": "ready", "database": "ok", "market_data": "ok"}


@app.get("/metrics")
async def metrics():
    return {"storage": "in_memory_process_local", "counters": metrics_snapshot()}


async def get_current_user(request: Request, session=Depends(get_session)):
    init_data = request.headers.get("X-Telegram-Init-Data") or ""
    if not init_data:
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "Missing initData"})
    parsed = validate_init_data(init_data, settings.bot_token)
    user_data = parse_user_from_init_data(parsed) if parsed else None
    if not user_data or not user_data.get("id"):
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "Invalid initData"})
    try:
        telegram_id = int(user_data["id"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "Invalid user"})
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND", "message": "Call Telegram auth first"})
    if user.is_banned:
        raise HTTPException(status_code=403, detail={"code": "USER_BANNED", "message": "Trading is unavailable"})
    return user


@app.post("/api/auth/telegram")
async def auth_telegram(request: Request, session=Depends(get_session)):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"code": "INVALID_JSON", "message": "Invalid request"})
    init_data = body.get("initData", "") if isinstance(body, dict) else ""
    parsed = validate_init_data(init_data, settings.bot_token)
    user_data = parse_user_from_init_data(parsed) if parsed else None
    if not user_data or not user_data.get("id"):
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "Invalid initData"})
    try:
        telegram_id = int(user_data["id"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "Invalid user"})

    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if user and user.is_banned:
        raise HTTPException(status_code=403, detail={"code": "USER_BANNED", "message": "Trading is unavailable"})
    if not user:
        user = User(
            telegram_id=telegram_id,
            username=user_data.get("username"),
            is_simulated=False,
        )
        session.add(user)
        await session.flush()
    account = await get_or_create_trading_account(session, user.id)
    increment("users_started")
    await session.commit()
    return {
        "user_id": user.id,
        "telegram_id": telegram_id,
        "trading_mode": settings.trading_mode,
        "account_id": account.id,
    }


@app.get("/api/me")
async def get_me(user=Depends(get_current_user)):
    return {"telegram_id": user.telegram_id, "username": user.username}


@app.get("/api/account")
async def get_account(user=Depends(get_current_user), session=Depends(get_session)):
    result = await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND"})
    return {
        "currency": account.currency,
        "initial_balance": str(account.initial_balance),
        "cash_balance": str(account.cash_balance),
        "equity": str(account.equity),
        "margin_used": str(account.margin_used),
        "available_margin": str(account.available_margin),
        "realized_pnl": str(account.realized_pnl),
        "unrealized_pnl": str(account.unrealized_pnl),
        "total_pnl": str(account.total_pnl),
        "trading_mode": settings.trading_mode,
    }


@app.get("/api/account/stats")
async def get_stats(user=Depends(get_current_user), session=Depends(get_session)):
    result = await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND"})
    query = await session.execute(select(PaperPosition).where(PaperPosition.account_id == account.id))
    positions = query.scalars().all()
    closed = [position for position in positions if position.status == "CLOSED"]
    winning = len([position for position in closed if position.realized_pnl > 0])
    return {
        "total_trades": len(positions),
        "open_trades": len(positions) - len(closed),
        "closed_trades": len(closed),
        "winning_trades": winning,
        "losing_trades": len(closed) - winning,
        "win_rate": (winning / len(closed) * 100) if closed else 0,
        "realized_pnl": str(account.realized_pnl),
        "unrealized_pnl": str(account.unrealized_pnl),
        "total_pnl": str(account.total_pnl),
    }


@app.get("/api/markets")
async def get_markets(session=Depends(get_session)):
    result = await session.execute(select(Instrument).where(Instrument.status == "active"))
    instruments = result.scalars().all()
    return [
        {"symbol": instrument.symbol, "base": instrument.base_asset, "quote": instrument.quote_asset}
        for instrument in instruments
    ]


@app.get("/api/markets/{symbol}/candles")
async def get_candles(
    symbol: str,
    timeframe: str = Query(default="1m", pattern="^(1m|5m|15m|1h|4h|1d)$"),
    limit: int = Query(default=100, ge=1, le=500),
):
    normalized = normalize_symbol(symbol)
    if normalized not in {"BTCUSDT", "ETHUSDT", "SOLUSDT"}:
        raise HTTPException(status_code=404, detail={"code": "INVALID_SYMBOL", "message": "Инструмент недоступен"})
    # No fabricated candle data: bounded parameters are accepted until a
    # shared candle store is implemented.
    return {
        "symbol": normalized,
        "timeframe": timeframe,
        "limit": limit,
        "candles": [],
        "market_data": "no_data",
    }


class OpenPositionRequest(BaseModel):
    symbol: str = Field(min_length=3, max_length=20)
    side: str = Field(min_length=4, max_length=5)
    quantity: Decimal = Field(gt=0)
    takeProfit: Decimal | None = Field(default=None, gt=0)
    stopLoss: Decimal | None = Field(default=None, gt=0)

    @field_validator("quantity", "takeProfit", "stopLoss")
    @classmethod
    def validate_decimal_input(cls, value: Decimal | None):
        if value is None:
            return value
        if not value.is_finite() or value.as_tuple().exponent < -12:
            raise ValueError("financial value must be finite and use at most 12 decimals")
        return value


def _trade_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, InsufficientMargin):
        return "INSUFFICIENT_MARGIN", "Недостаточно доступной маржи"
    if isinstance(exc, InvalidSymbol):
        return "INVALID_SYMBOL", "Инструмент недоступен"
    if isinstance(exc, InvalidQuantity):
        return "INVALID_QUANTITY", "Некорректный размер позиции"
    if isinstance(exc, InvalidTP_SL):
        return "INVALID_TP_SL", "Некорректные TP/SL"
    if isinstance(exc, PaperError):
        if "stale" in str(exc).lower():
            return "MARKET_DATA_STALE", "Рынок временно недоступен"
        if "unavailable" in str(exc).lower():
            return "MARKET_DATA_UNAVAILABLE", "Рынок временно недоступен"
        if "already" in str(exc).lower():
            return "ALREADY_PROCESSED", "Сделка уже обработана"
    return "TRADE_EXECUTION_FAILED", "Сделка не выполнена"


@app.post("/api/positions")
async def create_position(
    req: OpenPositionRequest,
    user=Depends(get_current_user),
    session=Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    if not idempotency_key:
        raise HTTPException(status_code=400, detail={"code": "IDEMPOTENCY_KEY_REQUIRED", "message": "Idempotency-Key required"})
    if not req.quantity.is_finite() or (req.takeProfit is not None and not req.takeProfit.is_finite()) or (req.stopLoss is not None and not req.stopLoss.is_finite()):
        raise HTTPException(status_code=400, detail={"code": "INVALID_NUMBER", "message": "Invalid numeric value"})
    result = await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND"})
    competition = await get_active_competition(session)
    if competition is None:
        raise HTTPException(status_code=409, detail={"code": "COMPETITION_ENDED", "message": "Турнир уже завершён"})
    try:
        await join_competition(session, user.id, competition.id)
        position = await open_position(
            session,
            account,
            normalize_symbol(req.symbol),
            req.side,
            req.quantity,
            req.takeProfit,
            req.stopLoss,
            idempotency_key=idempotency_key,
            competition_id=competition.id,
        )
        await session.commit()
        return {
            "id": position.id,
            "symbol": position.symbol,
            "side": position.side,
            "status": position.status,
            "quantity": str(position.quantity),
            "entryPrice": str(position.entry_price),
            "takeProfit": str(position.take_profit) if position.take_profit else None,
            "stopLoss": str(position.stop_loss) if position.stop_loss else None,
        }
    except Exception as exc:
        await session.rollback()
        code, message = _trade_error(exc)
        logger.warning("Paper open rejected: %s", type(exc).__name__)
        raise HTTPException(status_code=409 if code == "ALREADY_PROCESSED" else 400, detail={"code": code, "message": message})


@app.get("/api/positions")
async def list_positions(user=Depends(get_current_user), session=Depends(get_session)):
    result = await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))
    account = result.scalar_one_or_none()
    if not account:
        return []
    query = await session.execute(
        select(PaperPosition)
        .where(PaperPosition.account_id == account.id)
        .order_by(PaperPosition.opened_at.desc())
        .limit(50)
    )
    positions = query.scalars().all()
    return [
        {
            "id": position.id,
            "symbol": position.symbol,
            "side": position.side,
            "status": position.status,
            "quantity": str(position.quantity),
            "entry_price": str(position.entry_price),
            "current_price": str(position.current_price),
            "unrealized_pnl": str(position.unrealized_pnl),
            "realized_pnl": str(position.realized_pnl),
            "take_profit": str(position.take_profit) if position.take_profit else None,
            "stop_loss": str(position.stop_loss) if position.stop_loss else None,
            "opened_at": position.opened_at.isoformat(),
        }
        for position in positions
    ]


@app.post("/api/positions/{pos_id}/close")
async def close_pos(
    pos_id: int,
    user=Depends(get_current_user),
    session=Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    if not idempotency_key:
        raise HTTPException(status_code=400, detail={"code": "IDEMPOTENCY_KEY_REQUIRED", "message": "Idempotency-Key required"})
    result = await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND"})
    position = await session.get(PaperPosition, pos_id)
    if not position or position.account_id != account.id:
        raise HTTPException(status_code=404, detail={"code": "POSITION_NOT_FOUND"})
    try:
        position, pnl = await close_position(session, position, account, idempotency_key=idempotency_key)
        await session.commit()
        return {"id": position.id, "status": position.status, "realized_pnl": str(pnl)}
    except Exception as exc:
        await session.rollback()
        code, message = _trade_error(exc)
        logger.warning("Paper close rejected: %s", type(exc).__name__)
        raise HTTPException(status_code=409 if code == "ALREADY_PROCESSED" else 400, detail={"code": code, "message": message})


@app.get("/api/transactions")
async def get_transactions(user=Depends(get_current_user), session=Depends(get_session)):
    result = await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))
    account = result.scalar_one_or_none()
    if not account:
        return []
    query = await session.execute(
        select(PaperPosition)
        .where(PaperPosition.account_id == account.id)
        .order_by(PaperPosition.opened_at.desc())
        .limit(50)
    )
    positions = query.scalars().all()
    return [
        {
            "id": position.id,
            "symbol": position.symbol,
            "side": position.side,
            "status": position.status,
            "quantity": str(position.quantity),
            "entry_price": str(position.entry_price),
            "exit_price": str(position.current_price) if position.status == "CLOSED" else None,
            "notional": str(position.notional),
            "gross_pnl": str(position.realized_pnl) if position.status == "CLOSED" else str(position.unrealized_pnl),
            "fees": "0",
            "net_pnl": str(position.realized_pnl) if position.status == "CLOSED" else "0",
            "opened_at": position.opened_at.isoformat(),
            "closed_at": position.closed_at.isoformat() if position.closed_at else None,
        }
        for position in positions
    ]


@app.get("/api/profile")
async def get_profile(user=Depends(get_current_user), session=Depends(get_session)):
    increment("profile_viewed")
    account = (
        await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))
    ).scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND"})
    stats = await get_stats(user, session)
    return {
        "balance": str(account.cash_balance),
        "equity": str(account.equity),
        "available": str(account.available_margin),
        "pnl": str(account.total_pnl),
        **stats,
    }
