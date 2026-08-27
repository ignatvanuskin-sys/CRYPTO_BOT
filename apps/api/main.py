from fastapi import FastAPI, Depends, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from decimal import Decimal
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import select
from config import settings
from apps.api.auth import validate_init_data, parse_user_from_init_data
from db.models import User
from db.paper_models import TradingAccount, PaperPosition, Instrument, AccountLedger
from services.trading_account import get_or_create_trading_account
from services.paper_adapter import open_position, close_position
import hmac, hashlib, json

app = FastAPI(title="Trade CryptoBot API", version="0.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

engine = create_async_engine(settings.database_url_async, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

async def get_session():
    async with SessionLocal() as s:
        yield s

async def get_current_user(request: Request, session=Depends(get_session)):
    init_data = request.headers.get("X-Telegram-Init-Data") or request.headers.get("x-telegram-init-data") or ""
    if not init_data:
        # fallback to query param for dev
        init_data = request.query_params.get("initData", "")
    if not init_data:
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "Missing initData"})
    parsed = validate_init_data(init_data, settings.bot_token)
    if not parsed:
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "Invalid initData"})
    user_data = parse_user_from_init_data(parsed)
    if not user_data or not user_data.get("id"):
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "Invalid user"})
    telegram_id = int(user_data["id"])
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND", "message": "User not found, call /api/auth/telegram first"})
    return user

@app.post("/api/auth/telegram")
async def auth_telegram(request: Request, session=Depends(get_session)):
    body = await request.json()
    init_data = body.get("initData", "")
    parsed = validate_init_data(init_data, settings.bot_token)
    if not parsed:
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "Invalid initData"})
    user_data = parse_user_from_init_data(parsed)
    telegram_id = int(user_data["id"])
    username = user_data.get("username")
    first_name = user_data.get("first_name")
    # get or create user + trading account
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if not user:
        user = User(telegram_id=telegram_id, username=username)
        session.add(user)
        await session.flush()
    acc = await get_or_create_trading_account(session, user.id)
    await session.commit()
    return {"user_id": user.id, "telegram_id": telegram_id, "trading_mode": settings.trading_mode, "account_id": acc.id}

@app.get("/api/me")
async def get_me(user=Depends(get_current_user)):
    return {"telegram_id": user.telegram_id, "username": user.username}

@app.get("/api/account")
async def get_account(user=Depends(get_current_user), session=Depends(get_session)):
    result = await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))
    acc = result.scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND"})
    return {
        "currency": acc.currency,
        "initial_balance": str(acc.initial_balance),
        "cash_balance": str(acc.cash_balance),
        "equity": str(acc.equity),
        "margin_used": str(acc.margin_used),
        "available_margin": str(acc.available_margin),
        "realized_pnl": str(acc.realized_pnl),
        "unrealized_pnl": str(acc.unrealized_pnl),
        "total_pnl": str(acc.total_pnl),
        "trading_mode": settings.trading_mode,
    }

@app.get("/api/account/stats")
async def get_stats(user=Depends(get_current_user), session=Depends(get_session)):
    result = await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))
    acc = result.scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND"})
    # positions stats
    from sqlalchemy import func
    q = await session.execute(select(PaperPosition).where(PaperPosition.account_id == acc.id))
    positions = q.scalars().all()
    total = len(positions)
    closed = [p for p in positions if p.status == "CLOSED"]
    open_cnt = [p for p in positions if p.status == "OPEN"]
    winning = len([p for p in closed if p.realized_pnl > 0])
    losing = len([p for p in closed if p.realized_pnl <= 0])
    win_rate = (winning / len(closed) * 100) if closed else 0
    return {
        "total_trades": total,
        "open_trades": len(open_cnt),
        "closed_trades": len(closed),
        "winning_trades": winning,
        "losing_trades": losing,
        "win_rate": win_rate,
        "realized_pnl": str(acc.realized_pnl),
        "unrealized_pnl": str(acc.unrealized_pnl),
        "total_pnl": str(acc.total_pnl),
    }

@app.get("/api/markets")
async def get_markets(session=Depends(get_session)):
    result = await session.execute(select(Instrument).where(Instrument.status == "active"))
    insts = result.scalars().all()
    return [{"symbol": i.symbol, "base": i.base_asset, "quote": i.quote_asset} for i in insts]

@app.get("/api/markets/{symbol}/candles")
async def get_candles(symbol: str):
    # proxy to ccxt via pricing? For MVP return empty
    return {"symbol": symbol, "candles": []}

class OpenPositionRequest(BaseModel):
    symbol: str
    side: str
    quantity: float
    takeProfit: float | None = None
    stopLoss: float | None = None

@app.post("/api/positions")
async def create_position(req: OpenPositionRequest, request: Request, user=Depends(get_current_user), session=Depends(get_session)):
    idempotency_key = request.headers.get("Idempotency-Key") or request.headers.get("idempotency-key") or f"pos:{user.id}:{req.symbol}:{req.side}:{req.quantity}:{datetime.now(timezone.utc).timestamp()}"
    result = await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))
    acc = result.scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND"})
    try:
        pos = await open_position(session, acc, req.symbol, req.side, Decimal(str(req.quantity)),
                                   Decimal(str(req.takeProfit)) if req.takeProfit else None,
                                   Decimal(str(req.stopLoss)) if req.stopLoss else None,
                                   idempotency_key=idempotency_key)
        await session.commit()
        return {"id": pos.id, "symbol": pos.symbol, "side": pos.side, "status": pos.status, "quantity": str(pos.quantity), "entryPrice": str(pos.entry_price), "takeProfit": str(pos.take_profit) if pos.take_profit else None, "stopLoss": str(pos.stop_loss) if pos.stop_loss else None}
    except Exception as e:
        await session.rollback()
        code = "TRADE_EXECUTION_FAILED"
        msg = str(e)
        if "Insufficient" in msg:
            code = "INSUFFICIENT_MARGIN"
        elif "Invalid" in msg:
            code = "INVALID_QUANTITY"
        raise HTTPException(status_code=400, detail={"code": code, "message": msg})

@app.get("/api/positions")
async def list_positions(user=Depends(get_current_user), session=Depends(get_session)):
    result = await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))
    acc = result.scalar_one_or_none()
    if not acc:
        return []
    q = await session.execute(select(PaperPosition).where(PaperPosition.account_id == acc.id).order_by(PaperPosition.opened_at.desc()).limit(50))
    positions = q.scalars().all()
    return [{"id": p.id, "symbol": p.symbol, "side": p.side, "status": p.status, "quantity": str(p.quantity), "entry_price": str(p.entry_price), "current_price": str(p.current_price), "unrealized_pnl": str(p.unrealized_pnl), "realized_pnl": str(p.realized_pnl), "take_profit": str(p.take_profit) if p.take_profit else None, "stop_loss": str(p.stop_loss) if p.stop_loss else None, "opened_at": p.opened_at.isoformat()} for p in positions]

@app.post("/api/positions/{pos_id}/close")
async def close_pos(pos_id: int, request: Request, user=Depends(get_current_user), session=Depends(get_session)):
    idempotency_key = request.headers.get("Idempotency-Key") or f"close:{pos_id}:{datetime.now(timezone.utc).timestamp()}"
    result = await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))
    acc = result.scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND"})
    pos = await session.get(PaperPosition, pos_id)
    if not pos or pos.account_id != acc.id:
        raise HTTPException(status_code=404, detail={"code": "POSITION_NOT_FOUND"})
    if pos.status != "OPEN":
        raise HTTPException(status_code=400, detail={"code": "POSITION_ALREADY_CLOSED"})
    try:
        pos, pnl = await close_position(session, pos, acc, idempotency_key=idempotency_key)
        await session.commit()
        return {"id": pos.id, "status": pos.status, "realized_pnl": str(pnl)}
    except Exception as e:
        await session.rollback()
        raise HTTPException(status_code=400, detail={"code": "TRADE_EXECUTION_FAILED", "message": str(e)})

@app.get("/api/transactions")
async def get_transactions(user=Depends(get_current_user), session=Depends(get_session)):
    result = await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))
    acc = result.scalar_one_or_none()
    if not acc:
        return []
    q = await session.execute(select(PaperPosition).where(PaperPosition.account_id == acc.id).order_by(PaperPosition.opened_at.desc()).limit(50))
    positions = q.scalars().all()
    return [{"id": p.id, "symbol": p.symbol, "side": p.side, "status": p.status, "quantity": str(p.quantity), "entry_price": str(p.entry_price), "exit_price": str(p.current_price) if p.status=="CLOSED" else None, "notional": str(p.notional), "gross_pnl": str(p.realized_pnl) if p.status=="CLOSED" else str(p.unrealized_pnl), "fees": "0", "net_pnl": str(p.realized_pnl) if p.status=="CLOSED" else "0", "take_profit": str(p.take_profit) if p.take_profit else None, "stop_loss": str(p.stop_loss) if p.stop_loss else None, "opened_at": p.opened_at.isoformat(), "closed_at": p.closed_at.isoformat() if p.closed_at else None} for p in positions]

@app.get("/api/profile")
async def get_profile(user=Depends(get_current_user), session=Depends(get_session)):
    acc = (await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))).scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND"})
    stats = await get_stats(user, session)
    return {"balance": str(acc.cash_balance), "equity": str(acc.equity), "available": str(acc.available_margin), "pnl": str(acc.total_pnl), **stats}
