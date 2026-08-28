# Client-Ready Telegram Trading Game Report

## Product UX

Implemented locally without changing the trading architecture:

- Russian persistent Telegram main menu.
- `/start` landing screen with demo balance, current cup, timer, participant count, prize pool, rank and prize distribution.
- `/trade` flow: BTC/ETH/SOL -> LONG/SHORT -> size -> optional TP/SL -> confirmation.
- Server-side market snapshot display in the split bot/API/worker architecture.
- Positions screen with entry, current price, PnL, TP/SL distance and close confirmation.
- Profile screen with equity, ROI, rank, wins, losses, win rate and best/worst trade.
- Transaction history limited to the latest 10 closed positions.
- Competition details and Russian rules screen.
- Leaderboard with timer, prizes, user rank and race-to-TOP-10 message.
- Telegram competition deep-link sharing.
- Deterministic navigation callbacks and back buttons.

## Trading

- LONG OPEN uses BingX ASK.
- LONG CLOSE uses BingX BID.
- SHORT OPEN uses BingX BID.
- SHORT CLOSE uses BingX ASK.
- Frontend and Telegram callback data cannot set execution price, PnL, ROI or equity.
- Idempotency is required for financial mutations.
- Expired competitions reject new paper opens.
- Invalid, stale, future, inverted, zero, NaN and Infinity prices/inputs are rejected.

## Competition

- Active competition is server-owned.
- Expired competitions are finalized by `workers.competition_lifecycle`.
- Finalization closes open positions, builds the deterministic snapshot, assigns prizes and marks the competition finished.
- Repeated finalization is safe.

## Leaderboard

Ranking order:

1. ROI descending
2. Equity descending
3. joined_at ascending
4. user_id ascending

The Telegram screen shows TOP 10, prize information, the user's rank and distance to TOP 10.

## Prizes

Demo distribution is exactly `$100.00`:

- 1: `$50.00`
- 2: `$25.00`
- 3: `$15.00`
- 4-9: `$1.43`
- 10: `$1.42`

Prize rows are unique per competition/rank.

## Anti-cheat

Reviewed and covered:

- fake execution price: rejected/ignored;
- fake PnL, ROI, equity and rank: not accepted by mutation APIs;
- fake competition_id: not accepted from the Mini App trade request;
- wrong-position close: ownership check returns not found;
- stale market data: rejected;
- expired competition trade: rejected;
- duplicate open/close: idempotency and database constraints;
- TP/SL plus manual close: account lock/status/idempotency protections;
- fake Telegram init data: HMAC and auth_date validation;
- banned users: rejected by API auth.

## Telegram

- `bot.main` remains the only polling entrypoint.
- Importing `bot.main` has no polling side effect.
- PostgreSQL advisory lock prevents a second bot polling instance.
- The working Railway bot deployment was not changed.

## API

- `/health`: process/database status and `market_data: ok|no_data`.
- `/ready`: returns `503` until database and required fresh market snapshots are ready.
- `/metrics`: explicitly reports process-local in-memory metric storage.
- Candle API accepts only `1m|5m|15m|1h|4h|1d` and limits results to 500.
- Mini App authenticates through `/api/auth/telegram` before account access.

## Worker

`workers.main` orchestrates:

- BingX perpetual price polling;
- shared PostgreSQL market snapshots;
- TP/SL processing;
- competition lifecycle.

A PostgreSQL advisory lock keeps the worker singleton. Railway should run exactly one worker replica.

## BingX

The canonical data path is:

```text
BingX -> price poller -> PostgreSQL market_snapshots -> bot/API/worker
```

Every accepted snapshot contains bid, ask, last, exchange timestamp and received timestamp. No fake fallback price is generated in production PostgreSQL mode.

## Metrics

Implemented runtime counters:

```text
users_started
competition_joined
trade_flow_started
trade_flow_completed
trade_opened
trade_closed
tp_triggered
sl_triggered
competition_finished
leaderboard_viewed
profile_viewed
idempotency_hit
double_close_prevented
stale_price_rejected
bingx_error
```

They reset after process restart and are not persistent analytics.

## Demo Acceptance

Deterministic acceptance passed:

```text
A LONG OPEN  = 50010 (ASK)
B SHORT OPEN = 50000 (BID)
A LONG CLOSE = 50100 (BID)
B SHORT CLOSE = 50110 (ASK)
```

Result:

- A positive PnL and rank 1.
- B negative PnL and rank 2.
- Competition finished.
- Leaderboard snapshots created once.
- Prizes assigned once.
- Second finalization did not add snapshots or prizes.

## Tests

```text
23 passed
4 skipped
0 failed
```

The four skipped tests require a real PostgreSQL/Docker environment for `FOR UPDATE` race validation.

Additional checks passed:

- full Python compilation;
- bot/API/worker imports;
- combined Telegram dispatcher registration;
- fresh Alembic upgrade through revision 004;
- local Uvicorn startup;
- `/health` HTTP smoke test;
- `/ready` degraded response without market snapshots;
- bounded candles (`limit=500` accepted, `limit=501` rejected);
- security input validation;
- deterministic bid/ask demo acceptance.

## Railway

Intended topology:

```text
crypto-bot
  python -m bot.main
  replicas: 1

crypto-api
  uvicorn apps.api.main:app --host 0.0.0.0 --port $PORT

crypto-worker
  python -m workers.main
  replicas: 1
```

Run Alembic once through a Railway release/pre-deploy migration job. Do not run concurrent migrations from every replica.

## Migration

Added revision `004_runtime_safety`:

- `market_snapshots`;
- `competition_prizes`;
- `users.is_simulated`.

No legacy tables were deleted.

## Remaining Risks

- PostgreSQL race tests were skipped locally because no PostgreSQL/Docker test environment was available.
- Telegram result notifications are best-effort after financial commit; notification failure does not roll back money state.
- Runtime metrics are in-memory.
- Railway service splitting and one-off migration configuration still require manual platform setup.
- Legacy weekly handlers remain alongside the paper competition domain for compatibility.
- Only `python -m workers.main` should be used for the production worker; standalone legacy worker entrypoints do not provide the shared singleton orchestration.

## Files Changed

The working tree contains modified and new files for Telegram UX, API hardening, shared market data, worker orchestration, competition/demo/prize handling, notifications, migration 004 and tests. The exact uncommitted file list is available from `git status --short`.

## Git Status

NOT COMMITTED

## Deployment

NOT DEPLOYED
