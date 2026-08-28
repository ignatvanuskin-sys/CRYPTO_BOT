# Plan: feature/miniapp-paper-trading (TradeWeek → Paper Trading + Mini App)

## Inspect (done)
- **Preserve без переписывания:** `db/base.py`, `db/models.py` (users, assets, transactions ledger, positions old), `services/accounts.py` (ensure_can_trade, phone), `services/pricing.py` (cache + staleness), `services/trading.py` (FOR UPDATE, idempotency, NUMERIC), `services/weekly_cycle.py` (депрекейтим, не удаляем), `workers/price_poller.py` (ccxt BingX, retry/backoff, alert), `config.py` (env), `alembic` — всё остаётся, legacy-таблицы не дропаем.
- **Legacy ветка:** `legacy/tradeweek` сохранена, `feature/miniapp-paper-trading` от `main c87ca88` → `7370d9d` → `28d28c5`.
- **Weekly депрекейт:** новые `TradingAccount` не зависят от `weeks`; старый код остаётся но не вызывается для новых юзеров (флаг `TRADING_MODE`).

## Design: новые доменные модели (без удаления legacy)
```
trading_accounts (NEW)
  id PK, user_id FK UNIQUE, currency USD, initial_balance NUMERIC(18,2)=10000,
  cash_balance, equity, margin_used, available_margin, realized_pnl, unrealized_pnl, total_pnl  -- вычисляемые, но материализуем и сверяем с ledger
  created_at, updated_at
  UNIQUE(user_id)

instruments (NEW, переиспользуем assets как источник, но новая таблица для precision)
  symbol PK (BTCUSDT), base/quote, status active/delisted, price_precision, quantity_precision, min_quantity, max_quantity, created_at
  Seed: BTCUSDT, ETHUSDT, SOLUSDT (расширяемо без кода)

account_ledger (NEW, аналог transactions но per account)
  id PK, account_id FK, type ENUM(INITIAL_BALANCE,TRADE_OPEN,TRADE_CLOSE,FEE,ADJUSTMENT), amount NUMERIC(18,2), balance_after NUMERIC(18,2) CHECK>=0, reference_type/ref_id, created_at, idempotency_key UNIQUE

orders (NEW, paper)
  id PK, account_id FK, position_id FK nullable, symbol FK, side LONG/SHORT, order_type MARKET, quantity NUMERIC(30,12), requested_price, executed_price NUMERIC(30,12), status PENDING/FILLED/REJECTED, reduce_only bool, idempotency_key UNIQUE, created_at, executed_at, rejection_reason

positions (NEW, id PK — не composite)
  id PK, account_id FK, symbol FK, side LONG/SHORT, status OPEN/CLOSING/CLOSED/CANCELLED, quantity NUMERIC(30,12) CHECK>0, entry_price, current_price, notional, take_profit, stop_loss, realized_pnl, unrealized_pnl, fee_open/close NUMERIC(18,2) DEFAULT 0, opened_at, closed_at, updated_at
  INDEX(account_id, status), UNIQUE(account_id, symbol) WHERE status='OPEN' ? нет, допускаем несколько позиций по символу
```
- Старые `transactions` / `positions (user_id,week_id,symbol)` / `weeks` / `leaderboard_snapshots` / `prizes` — **не удаляем**, миграция `DROP` только в отдельном PR после проверки.

## Migrations
- `alembic revision 002_paper_trading` — создать 4 новые таблицы, индексы, `NUMERIC` без `float`, `CHECK`, `UNIQUE` idempotency. Не трогать старые.
- Конфиг: `TRADING_MODE=paper`, `INITIAL_BALANCE_USD=10000`, `PAPER_SLIPPAGE_BPS=5` в `config.py` + `database_url_async`.

## Backend
- `services/accounts.py` + `get_or_create_trading_account()` — идемпотентное создание аккаунта на `/start`, начисление `INITIAL_BALANCE` один раз (idempotency_key `init:{account_id}`), проверка `status`.
- `services/exchange/paper_adapter.py` — `PaperExchangeAdapter` с методами `createOrder, getTicker, getPrice, getCandles, closePosition` — единственный кто трогает ledger/позиции, единый источник истины.
- `services/positions.py` — открытие `POST /positions`: валидация symbol/quantity/TP/SL (LONG TP>entry, SL<entry и наоборот), `required_margin = notional` (без плеча) <= `available_margin`, `SELECT FOR UPDATE` на `trading_accounts`, создание `order` + `position` + `ledger TRADE_OPEN`, цена `executed_price = latest_market_price` из `pricing` (не от frontend), `slippage` опционально.
- `services/pnl.py` — `gross = (exit-entry)*qty` для LONG, `(entry-exit)*qty` для SHORT, `net = gross - fees`, `unrealized` пересчёт при каждом тике.
- Сохраняем `FOR UPDATE`, `idempotency_key` (header `Idempotency-Key`), `balance_after >=0`, `DECIMAL`.

## TP/SL Worker
- `workers/tp_sl_engine.py` — отдельный async loop: каждую секунду `getPrice` для всех `OPEN` позиций, `update current_price + unrealized_pnl`, проверка
  ```
  LONG: price >= TP -> close, price <= SL -> close
  SHORT: price <= TP -> close, price >= SL -> close
  ```
  закрытие через `PaperExchangeAdapter.closePosition` с `FOR UPDATE` + `ledger TRADE_CLOSE`, нотификация `NotificationService` (Telegram).
- Reconciliation job `equity == balance + unrealized_pnl`.

## API
- `api/auth.py` — `POST /api/auth/telegram` валидация `initData` (HMAC SHA256 с `BOT_TOKEN`, не `initDataUnsafe`), `telegram_id` только с сервера.
- `api/{me,account,account/stats,markets,markets/:symbol/candles,positions,positions/:id/close,transactions,profile}` — все через `PaperExchangeAdapter`, ошибки структурированы `INSUFFICIENT_MARGIN` etc (`AGENTS:38`).
- Разделение `apps/api` (FastAPI) + `apps/bot` (aiogram) — оба используют те же сервисы.

## Mini App
- `apps/miniapp` (Vite + React): `/trade` (asset selector 3-10 символов, chart TradingView Lightweight, `LONG/SHORT`, size, TP/SL, `OPEN POSITION` с `Confirm`), `/transactions` (OPEN/CLOSED, filters, pagination 20), `/profile` (balance/equity/pnl/win_rate), `apps/bot` deep links `/trade?symbol=SOLUSDT`.
- `trading_mode` badge `PAPER TRADING`.

## Tests
- Unit: PnL LONG/SHORT profit/loss, TP/SL LONG/SHORT, balance open/close profit/loss, win_rate.
- Integration: `/start` idempotency, `open LONG → TP → profile updated`, double close race with `FOR UPDATE`, insufficient margin, invalid symbol/quantity, market_data_unavailable.
- Negative: из `AGENTS:60`.

## Verification (DoD AGENTS:62)
- Запустить backend + bot + miniapp, пройти `/start → $10k → /trade SOL LONG → /transactions → price move → TP → /profile`, проверить повторный клик idempotency, ошибки, Telegram auth, `pytest` green, secrets не в git, Railway `dynamic-curiosity` не трогать без approval.
