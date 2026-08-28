# CryptoBot — Paper Trading Demo на BingX: от А до Я

> **Статус:** `main` — единый Railway-сервис `python -m bot.main`, демо-баланс $10 000, торговля LONG/SHORT по живым ценам BingX Perpetual, `paper_positions` + `market_snapshots`, `price_poller/tp_sl_engine/competition_lifecycle` — фоновые `asyncio.create_task` в том же процессе. Легаси TradeWeek сохранён в ветке `legacy/tradeweek-snapshot`.
> **Бот:** `@crypto_demo_vbot` (id `8610467759`)
> **Проект Railway:** `dynamic-curiosity` (`01e288e4-99a1-4602-b9c9-81549f951b70`), сервис `CRYPTO_BOT` + `Postgres`

---

## 1. Идея и сценарий (референс ТЗ)

```
/start → проверка + request_contact → демо-баланс $10 000 (идемпотентно) → главное меню [Личный кабинет]
/profile → юзернейм, баланс (equity), успешных/неуспешных, общий ROE, место в рейтинге
/transactions → все сделки (открытые и закрытые, до 15, с PnL и кнопкой Закрыть)
/trade → 1) Выбрать монету → ввод тикера → ссылка на график BingX https://bingx.com/en/perpetual/{BASE}-USDT
        → 2) Быстрое открытие → тикер → бюджет (маржа) → плечо → LONG/SHORT → TP/SL (опционально) → подтверждение → open_position()
```

Никаких Mini App, вкладок сверх схемы. ASK/BID-правила, `Decimal`, ledger, отказ при `Market data stale/unavailable` — сохранены.

---

## 2. Стек

| Слой | Технология | Версия / Примечание |
|------|------------|---------------------|
| Язык | Python | `3.11.11` (`runtime.txt`, `nixpacks.toml: python311`) |
| Бот | aiogram | `3.22.0`, `ParseMode.HTML`, `DefaultBotProperties` |
| ORM | SQLAlchemy | `2.0.51` async, `asyncpg 0.30.0` / `aiosqlite 0.20.0` |
| Миграции | Alembic | `1.15.2`, `alembic/env.py` |
| Биржа | ccxt | `4.3.89` `ccxt.bingx` (`defaultType: swap`) |
| Конфиг | pydantic-settings | `2.10.1`, `pydantic 2.11.7` |
| HTTP | httpx | `0.27.2` |
| Тесты | pytest + pytest-asyncio | `9.1.1` / `1.4.0` |
| Деплой | Railway | `Railpack` builder, `Procfile`, `nixpacks.toml`, `Postgres 18` |

---

## 3. Архитектура — один процесс

```
┌─────────────────────────────────────────────┐
│  bot/main.py (single process, LOCK_KEY)     │
│  ├─ aiogram Dispatcher + db_middleware      │
│  │   ├─ profile_router (/start, /profile, /transactions) │
│  │   ├─ trade_router (/trade, ticker→budget→leverage→side→TP/SL→confirm, close) │
│  │   └─ admin_router                        │
│  ├─ asyncio.create_task(price_poller)       │  BingX fetch_tickers → market_snapshots
│  ├─ asyncio.create_task(tp_sl_engine)       │  bid/ask → TP/SL → close_position
│  └─ asyncio.create_task(competition_lifecycle) │  finalize_expired_competitions
└─────────────────────────────────────────────┘
         │ PostgreSQL advisory lock 82463518 (workers/lock.py)
         └─ postgres.railway.internal:5432/railway
```

Один `BOT_TOKEN`, один `DATABASE_URL`, один `LOCK_KEY=82463518`. Воркеры не отдельный процесс — устраняет причину провала «написанный код не задеплоен».

---

## 4. Структура папок

```
C:\TGOD\CryptoBot\
├── alembic/                      # миграции
│   ├── env.py                    # env + target_metadata = Base.metadata (db.models, paper_models, competition_models, market_data)
│   ├── script.py.mako
│   └── versions/
│       ├── 001_initial.py        # users, weeks, assets, transactions, orders, positions, leaderboard_snapshots, prizes
│       ├── 002_paper_trading.py  # trading_accounts, instruments, account_ledger, paper_positions, paper_orders, audit_logs + seed BTC/ETH/SOL
│       ├── 003_competition.py    # competitions, competition_participants, executions, leaderboard snapshots (paper)
│       ├── 004_runtime_safety.py # is_simulated, market_snapshots, competition_prizes
│       ├── 005_paper_leverage.py # paper_positions.leverage
│       ├── 006_widen_symbols.py  # VARCHAR(20)→40 (NCSINASDAQ1002USDUSDT 22 символа)
│       └── 007_instrument_max_leverage.py # instruments.max_leverage (BTC/ETH 300, SOL 100, остальные 50)
├── bot/
│   ├── main.py                   # 92 строки, single-process, retry lock 15×2с, DefaultBotProperties(HTML)
│   ├── emojis.py                 # 77 строк, все premium ID из ТЗ, tg_emoji() + TG_LONG/TG_SHORT/... (LONG 5449683594425410231, SHORT 5447183459602669338)
│   ├── views.py                  # main_menu (ReplyKeyboard premium), back_keyboard, fmt_money/fmt_price/format_side, bingx_chart_url, get_display_snapshot
│   ├── keyboards.py              # contact_keyboard (request_contact + ENVELOPE_ID)
│   └── handlers/
│       ├── __init__.py           # re-export admin_router
│       ├── profile.py            # 262 строки, /start+contact→grant+competition, /profile, /transactions, nav:home/profile/transactions (исправлен callback.from_user баг)
│       ├── trade.py              # 617 строк, trade_state dict, LEVERAGES 1..300, safe_trade_error (HTML), confirm→open_position, close_preview/confirm
│       └── admin.py              # admin_stats/ban/unban/active_competition/create_demo_cup/seed_demo_players/reconcile/finish_competition/product_stats
├── config.py                     # Settings (BOT_TOKEN, DATABASE_URL, INITIAL_BALANCE_USD=10000, MARKET_DATA_MAX_AGE_MS=10000, PRICE_POLL_INTERVAL=2, ADMIN_IDS, DEMO_*)
├── db/
│   ├── base.py                   # declarative_base()
│   ├── __init__.py               # Base + User
│   ├── models.py                 # User (telegram_id unique, phone unique, is_banned, is_simulated)
│   ├── paper_models.py           # TradingAccount, Instrument (max_leverage), AccountLedger, PaperOrder, PaperPosition (leverage), AuditLog
│   ├── competition_models.py     # Competition, CompetitionParticipant, Execution, LeaderboardSnapshot, CompetitionPrize
│   └── market_data.py            # MarketSnapshot (symbol PK 40, bid/ask/last, exchange_timestamp, received_at)
├── services/
│   ├── accounts.py               # get_or_create_user, verify_phone, ensure_can_trade (ban+phone)
│   ├── trading_account.py        # get_or_create_trading_account (idempotent, ledger INITIAL_BALANCE), refresh_account_stats (equity=cash+margin+unrealized)
│   ├── paper_adapter.py          # open_position/close_position (509 строк): ASK/BID, slippage, quantity/notional, leverage, max_leverage check, TP/SL validate, margin, ledger, Execution, idempotency savepoint, FOR UPDATE lock
│   ├── bingx_market_data.py      # PriceSnapshot, normalize_symbol, validate_snapshot, is_stale, persist_snapshot, get_shared_snapshot (PostgreSQL), get_execution_snapshot (shared+sqlite fallback)
│   ├── competition.py            # get_active_competition, get_or_create_default_competition (savepoint), join_competition, update_participant_equity, finish_competition
│   ├── leaderboard.py            # build_leaderboard (ROI sort), get_top_n, get_user_rank, snapshot_leaderboard
│   ├── pnl.py                    # calc_pnl, calc_unrealized, calc_notional (Decimal 0.01)
│   ├── demo.py                   # DEMO_PRIZES 10×$100, create_demo_cup, seed_demo_players
│   ├── notifications.py          # notify_competition_finished (best-effort Bot.send_message)
│   └── metrics.py                # in-memory Counter increment/snapshot/reset
├── workers/
│   ├── lock.py                   # LOCK_KEY 82463518, acquire/release advisory_lock (connection-scoped)
│   ├── price_poller.py           # 153 строки, sync_instruments (price/quantity precision, max_leverage), fetch_once batch commit, run_forever
│   ├── tp_sl_engine.py           # check_and_close_positions (savepoint per close), run_forever 1с
│   └── competition_lifecycle.py  # finalize_competition_session, finalize_expired_competitions (savepoint per competition), run_forever 10с
├── tests/
│   ├── conftest.py               # sqlite_engine, pg_engine (TEST_DATABASE_URL→testcontainers), autouse clear _price_cache
│   ├── test_paper_mvp.py         # bid/ask, stale, competition ended
│   ├── test_paper_money.py       # leverage margin, insufficient, invalid leverage, idempotent grant
│   ├── test_shared_market.py     # shared snapshot source of truth, stale/future, finalize skip closed
│   ├── test_demo_acceptance.py   # LONG ASK / SHORT BID, prizes, finalize idempotent
│   ├── test_paper_race_pg.py     # PG race same key→1 position, one close, one snapshot
│   ├── test_product_acceptance.py# premium icons, ticker normalize, chart URL, safe_trade_error HTML, LONG/SHORT IDs
│   └── test_tp_sl_leverage_price.py # 10 тестов: format_side, fmt_price, per-coin leverage, TP/SL triggers, low-price UB
├── docs/ANTI_CHEAT_AUDIT.md
├── outputs/                      # старые отчёты (BOT_FROM_A_TO_Z..., FINAL..., client-ready)
├── ACCEPTANCE_EVIDENCE.md        # прод-прогон на Railway (951 снапшот, 2.1с age, acceptance script)
├── MANUAL_TESTING.md             # чек-лист раздела 3
├── CRYPTOBOT_A_TO_Z.md           # этот файл
├── Procfile                      # web: PYTHONPATH=/app alembic upgrade head && PYTHONPATH=/app python -m bot.main
├── nixpacks.toml                 # [phases.setup] python311, [phases.install] pip install -r requirements.txt, [start] alembic...
├── runtime.txt                   # python-3.11.11
├── requirements.txt              # в т.ч. ccxt, aiogram, asyncpg
├── pytest.ini                    # asyncio_mode = auto
├── .env.example                  # шаблон без секретов
├── alembic.ini
└── tradeweek.db                  # локальный sqlite (игнор)

---

## 5. Конфигурация (`config.py:5`)

| ENV | Default | Назначение |
|-----|---------|------------|
| `BOT_TOKEN` | `""` | aiogram Bot |
| `DATABASE_URL` | `sqlite+aiosqlite:///./paper.db` | `postgresql+asyncpg` в проде, `database_url_async` конвертирует `postgresql://` |
| `REQUIRE_POSTGRES` | `false` | fail если не Postgres |
| `INITIAL_BALANCE_USD` | `10000` | демо-грант |
| `MARKET_DATA_MAX_AGE_MS` | `10000` | stale-порог для `get_execution_snapshot` (был 2000, поднят из-за 4с цикла 951 тикера) |
| `PRICE_POLL_INTERVAL_SECONDS` | `2` | `poll_prices` sleep |
| `BINGX_MARKET_TYPE` | `perpetual` | `swap` для ccxt |
| `PAPER_SLIPPAGE_BPS` | `0` | |
| `ADMIN_TELEGRAM_IDS` | `""` | comma `1,2,3` → `admin_ids_set` |
| `DEMO_SEED_ENABLED` | `false` | `admin_create_demo_cup/seed_demo_players` |
| `DEMO_PLAYER_COUNT` | `20` | |
| `DEMO_CUP_DURATION_HOURS` | `24` | |
| `DEMO_PRIZE_POOL` | `100` | |

`extra="ignore"` — старые `TRADING_MODE/WEEKLY_GRANT/PRIZE_TOP_N/WEBAPP_URL` игнорируются.

---

## 6. База данных

### 6.1 `users` (`db/models.py:14`)
`id PK`, `telegram_id BIGINT UNIQUE`, `username`, `phone_number TEXT UNIQUE`, `phone_verified_at`, `is_banned`, `is_simulated`, `ban_reason`, `created_at`. FK для всех остальных.

### 6.2 `instruments` (`paper_models.py:66`)
`symbol PK VARCHAR(40)` (BTCUSDT), `base_asset/quote_asset`, `status` enum, `price_precision/quantity_precision INT`, `min_quantity NUMERIC(30,12)`, `max_quantity`, `max_leverage INT default 50` (007: BTC/ETH 300, SOL 100), `created_at`.

### 6.3 `trading_accounts` (`paper_models.py:50`)
`id PK`, `user_id UNIQUE FK`, `currency USD`, `initial_balance/cash_balance/equity/margin_used/available_margin/realized_pnl/unrealized_pnl/total_pnl NUMERIC(18,2)`, `created_at/updated_at`. Один счёт на юзера, `equity = cash + margin_used + unrealized`.

### 6.4 `account_ledger` (`paper_models.py:78`)
`id PK`, `account_id FK`, `type` enum `INITIAL_BALANCE/TRADE_OPEN/TRADE_CLOSE/FEE/ADJUSTMENT`, `amount/balance_after NUMERIC(18,2)`, `reference_type/reference_id`, `idempotency_key TEXT UNIQUE`, `created_at`. `balance_after >=0`.

### 6.5 `paper_positions` (`paper_models.py:113`)
`id PK`, `account_id FK`, `competition_id FK`, `symbol FK 40`, `side` enum LONG/SHORT, `status` enum OPEN/CLOSED..., `quantity/entry_price/current_price NUMERIC(30,12)`, `notional NUMERIC(18,2)` (=price*qty), `leverage NUMERIC(10,2) default 1`, `take_profit/stop_loss`, `realized/unrealized_pnl`, `fee_open/close`, `opened_at/closed_at`. Индексы `account_status`, `symbol`, `competition`.

### 6.6 `paper_orders` (`paper_models.py:95`)
`id PK`, `account_id FK`, `position_id FK`, `symbol FK 40`, `side/order_type/status/reduce_only`, `quantity/requested/executed_price NUMERIC(30,12)`, `idempotency_key UNIQUE`, `rejection_reason`, `created_at/executed_at`.

### 6.7 `market_snapshots` (`market_data.py:16`)
`symbol PK 40`, `source BINGX`, `market_type PERPETUAL`, `bid/ask/last NUMERIC(30,12)`, `exchange_timestamp/received_at/updated_at TIMESTAMPTZ`, `CHECK ask>=bid`.

### 6.8 `competitions` (`competition_models.py:25`)
`id PK`, `name`, `status` UPCOMING/ACTIVE/FINISHED/CANCELLED, `starts_at/ends_at`, `initial_balance/prize_pool`, `ranking_metric ROI`, `price_source BINGX`, `market_type USD_M_PERPETUAL`.

### 6.9 `competition_participants` (`competition_models.py:39`)
`id PK`, `competition_id FK`, `user_id FK`, `starting/current_equity`, `realized/unrealized_pnl`, `roi NUMERIC(10,4)`, `rank`, `joined_at`, `UNIQUE(competition_id,user_id)`.

### 6.10 `executions` (`competition_models.py:57`)
`id PK`, `position_id FK`, `user_id FK`, `competition_id FK`, `symbol/side`, `price_source/market_type`, `bid/ask/execution_price/quantity/notional`, `market_timestamp/requested_at/executed_at`, `execution_reason OPEN/MANUAL_CLOSE/TAKE_PROFIT/STOP_LOSS`.

### 6.11 `competition_leaderboard_snapshots` / `competition_prizes` / `audit_logs`

Миграции `001→007` (Alembic). `Base.metadata.create_all` в тестах, `alembic upgrade head` в проде (`Procfile`).

---

## 7. Services — денежная логика

### `services/paper_adapter.py` (509 строк)
- `PaperError`, `InsufficientMargin`, `InvalidSymbol/Quantity/TP_SL`
- `_lock_account` — `SELECT ... FOR UPDATE` на `trading_accounts` (Postgres), no-op на sqlite
- `_validate_tp_sl(side, entry, tp, sl)` — LONG TP>entry SL<entry, SHORT наоборот
- `_resolve_idempotent_position` — по `idempotency_key` находит `PaperOrder` → `PaperPosition`
- `open_position(session, account, symbol, side, quantity/notional, tp/sl, idempotency_key!, notional, competition_id, requested_at, leverage=1)`:
  1. idempotency pre-check → `idempotency_hit`
  2. competition active (`starts_at<=now<ends_at`) или `Competition ended`
  3. `side` LONG/SHORT, `leverage` `1..300`, `Instrument` active (dash fallback)
  4. `get_execution_snapshot(session, symbol, market_data_max_age_ms)` → `snap.bid/ask` → **LONG OPEN=ASK, SHORT OPEN=BID**
  5. slippage, `quantity = notional/price` или `quantity`, `min/max` checks, `_validate_tp_sl`
  6. `await _lock_account`, `refresh(account)`, re-check idempotency (race)
  7. `notional = price*qty`, `required_margin = notional/leverage`, `available_margin` check → `REJECTED` order + `InsufficientMargin`
  8. `begin_nested()` savepoint: `PaperOrder FILLED` + `PaperPosition OPEN` (notional, leverage) + `Execution(OPEN)` → `cash_balance-=margin`, `margin_used+=margin`, `AccountLedger TRADE_OPEN -margin`, `refresh_account_stats` → `trade_opened`
- `close_position(session, position, account, idempotency_key!, reason=manual/TP/SL)`:
  1. idempotency pre-check (retry → same `realized_pnl`)
  2. `status==OPEN` else `Position not open`
  3. `get_execution_snapshot` → **LONG CLOSE=BID, SHORT CLOSE=ASK** → `close_price`
  4. `lock`, `refresh`, `qty` PnL `calc_pnl(side, entry, close, qty)` → `net = gross - fees`
  5. `begin_nested()` → `PaperOrder FILLED reduce_only` → `position CLOSED`, `current_price=close`, `realized_pnl=net`, `Execution(MANUAL_CLOSE/TAKE_PROFIT/STOP_LOSS)` → `returned_margin = notional/leverage`, `return_amount = margin+net`, `cash+=return`, `margin_used-=margin`, `realized_pnl+=net`, `AccountLedger TRADE_CLOSE +return`, `refresh_account_stats` → `trade_closed`

### `services/trading_account.py`
- `get_or_create_trading_account(user_id)` — `SELECT` else `begin_nested()` insert `TradingAccount` + `AccountLedger INITIAL_BALANCE` (idempotent, `IntegrityError` → re-read)
- `refresh_account_stats(account)` — `sum(unrealized_pnl)` по OPEN, `equity=cash+margin+unrealized`, `available_margin=cash`, `total_pnl=realized+unrealized`

### `services/bingx_market_data.py` (187 строк)
- `PriceSnapshot(symbol, bid/ask/last, exchange_timestamp, received_at, source=BINGX)`
- `normalize_symbol("BTC/USDT:USDT"→"BTCUSDT")`, `_cache_key("BINGX:PERPETUAL:BTC-USDT")`, `_price_cache` (3 ключа), `get_snapshot`, `update_snapshot`
- `validate_snapshot` — bid/ask/last finite >0, `ask>=bid`, `exchange_timestamp` не в будущем
- `is_stale(snapshot, max_age_ms)` — `now - exchange_timestamp > max_age` или `<-5с`
- `persist_snapshot(session, snap)` — upsert `MarketSnapshot` (validate)
- `get_shared_snapshot(session, symbol, max_age_ms)` — `SELECT MarketSnapshot` → validate → stale check
- `get_execution_snapshot(session, symbol, max_age_ms)` — `get_shared_snapshot` или (sqlite) `get_snapshot` fallback

### `services/competition.py`
- `get_active_competition`, `get_or_create_default_competition` (`pg_advisory_xact_lock 82463519`, `begin_nested` savepoint на race), `join_competition` (savepoint, `competition_joined`), `update_participant_equity` (`current_equity=cash+margin+unrealized`, `roi`), `finish_competition` (freeze `FINISHED`, `build_leaderboard`, `snapshot_leaderboard` один раз, `CompetitionPrize` для `DEMO TRADING CUP`)

### `services/leaderboard.py`
- `build_leaderboard` — пересчёт `current_equity/roi` для всех `CompetitionParticipant`, сортировка `roi→equity→joined_at→user_id`, проставляет `rank`
- `get_top_n`, `get_user_rank` (с `need_for_top10`), `snapshot_leaderboard`

### `services/pnl.py`
- `calc_pnl(side, entry, exit, qty)` → `(exit-entry)*qty` LONG, `(entry-exit)*qty` SHORT, `quantize 0.01`
- `calc_unrealized`, `calc_notional(price*qty)`

### `services/accounts.py`
- `get_or_create_user(telegram_id, username)`, `verify_phone(user, phone)`, `ensure_can_trade(user)` (ban/phone)

### `services/demo.py`
- `DEMO_PRIZES=[50,25,15,1.43×6,1.42]` sum 100, `create_demo_cup` (reuse пустого ACTIVE), `seed_demo_players` (telegram_id `-900...`, `is_simulated`)

### `services/metrics.py` / `notifications.py`
- `Counter increment/snapshot/reset` (thread-safe `Lock`)
- `notify_competition_finished(engine, competition_id)` — best-effort `Bot.send_message` топу

---

## 8. Bot

### `bot/main.py:23`
- `Bot(token, DefaultBotProperties(parse_mode=HTML))`, `create_async_engine(database_url_async)`, `acquire_advisory_lock(LOCK_KEY)` с ретраем `15×2с` (rolling deploy), `async_sessionmaker`, `Dispatcher` + `db_middleware(session)` с `try/rollback`, `include_router(profile/trade/admin)`, `create_task(price_poller/tp_sl_engine/competition_lifecycle)`, `asyncio.wait(FIRST_COMPLETED)` + graceful cancel/release.

### `bot/emojis.py`
- Все ID из ТЗ: `LONG 544968...`/`SHORT 544718...` + 40 остальных, `tg_emoji(id, fallback)` → `<tg-emoji emoji-id="...">...</tg-emoji>`, константы `TG_LONG` etc., fallback обычные emoji только внутри тега.

### `bot/views.py`
- `main_menu()` — `ReplyKeyboardMarkup` `Торговать` (`CHART_UP_ID`) / `Личный кабинет` (`CROWN_ID`)
- `back_keyboard(target)` — `InlineKeyboardButton Назад` (`PIN_ID`)
- `fmt_money` (`$1,234.56`), `fmt_price(value, precision)` (авто: `>=1000→2`, `>=1→4`, `>=0.1→6`, `<0.1→8` знаков, или `quantize` по `price_precision`), `format_side(side)` (`PositionSide` enum→`LONG`/`SHORT`), `bingx_chart_url(symbol)` → `https://bingx.com/en/perpetual/{BASE}-USDT`, `get_display_snapshot(session, symbol)` (`market_data_max_age_ms`)

### `bot/keyboards.py`
- `contact_keyboard()` — `KeyboardButton Поделиться номером request_contact + ENVELOPE_ID`

### `bot/handlers/profile.py` (262 строки)
- `_get_user_by_telegram_id`, `_grant_demo_balance`, `_ensure_competition`, `send_main_menu`
- `cmd_start` — `get_or_create_user`, если `phone_verified_at is None` → `contact_keyboard`, иначе grant+competition → `TG_PARTY/ TG_MONEY/ TG_CROWN` приветствие
- `handle_contact` — `contact.user_id==from_user.id`, `verify_phone` + grant + `join_competition`, `unique` → `Этот номер уже...`, иначе `TG_CHECK` подтверждение
- `_send_profile` / `cmd_profile` (`/profile`, `Личный кабинет`) — `TradingAccount`, `PaperPosition` (wins `realized>0`), `get_user_rank` → `TG_CROWN ЛИЧНЫЙ КАБИНЕТ`, `TG_MONEY баланс`, `TG_CHART сделки`, `TG_STAR rank`, inline `Сделки`/`Торговать` (premium icons)
- `_send_transactions` / `cmd_transactions` (`/transactions`, `Сделки`) — `PaperPosition` 15 последних, `format_side`, `fmt_price` для цен (исправлен `PositionSide.LONG` баг и `$0.14` → `$0.140000` для `UB`), `GREEN/RED` статус, inline `Закрыть {symbol} {side}` (`RED_ID`) + `Торговать`
- `nav_home/profile/transactions` — `callback.from_user.id` (исправлен баг `callback.message.from_user`), `trade_state` очистка, `ParseMode.HTML`

### `bot/handlers/trade.py` (617 строк)
- `trade_state: dict[int,dict]`, `LEVERAGES=["1","2","5","10","20","50","100","150","300"]` (расширено под 300x), `TG_*` константы, `_strip_tags`, `safe_trade_error` (HTML с `WARNING_ID`)
- `trade_menu_keyboard` — `Выбрать монету` (`DIAMOND_ID`), `Быстрое открытие` (`BOOM_ID`)
- `leverage_keyboard` — 2 ряда (`GEAR_ID`), `side_keyboard` — `LONG`/`SHORT` (`LONG/SHORT ID`), `tp_sl_keyboard` — `STAR_ID`/`FREE_ID`, `confirm_keyboard` — `CHECK_ID`
- `normalize_ticker`, `_validate_instrument`, `_account_line`
- `cmd_trade` (`/trade`, `Торговать`) — меню, `nav_trade` (clear trade_state)
- `cb_coin_select`/`cb_quick_open` — `awaiting ticker_chart/ticker_trade`
- `handle_trade_text` (`F.text`) — единая точка ввода: `from_user None` guard, `/` и `Личный кабинет/Сделки/Торговать` → `pop` + return, `ticker_chart`→ validate `Instrument` → `get_display_snapshot` → `fmt_price` + `bingx_chart_url` + `Открыть сделку` (`BOOM_ID`), `ticker_trade`→`awaiting budget` + `_account_line`, `budget`→`awaiting leverage` + `leverage_keyboard`, `tp_sl`→`skip` или `TP SL` → `_show_confirmation`
- `_show_confirmation` — `snapshot.ask/bid`, `notional=budget*leverage`, `side_tag TG_LONG/TG_SHORT`, `state_line` цена входа, `TG_STAR TP`/`RED SL` via `fmt_price`, `confirm_keyboard`
- `cb_quick_symbol`/`cb_re_leverage`/`cb_leverage`/`cb_side`/`cb_tp_sl` — все с `from_user/message is None` guard, `edit_text` HTML
- `cb_confirm` (`trade:confirm`) — `in_flight` guard, `ensure_can_trade`, `get_or_create_trading_account`, `get_or_create_default_competition`, `join_competition`, `open_position(..., leverage, notional=budget*leverage, idempotency_key=tg:{callback.id})` → `update_participant_equity`, `commit`, `pop` state, `TG_CHECK ПОЗИЦИЯ ОТКРЫТА` + `Мои сделки`/`Торговать` inline; `except` → `safe_trade_error` HTML + `_strip_tags` для alert
- `cb_cancel` — `pop` + `Сделка отменена.`
- `cb_close_preview`/`cb_close_confirm` (`close_preview:{id}`/`close_confirm:{id}`) — `from_user/message` guard, `get_display_snapshot`, `bid` LONG / `ask` SHORT, `calc PnL`, `TG_SIREN ЗАКРЫТИЕ`, `Да, закрыть` (`CHECK_ID`) / `Отмена` (`CROSS_ID`), `close_position(..., tg_close:{id})` → `TG_CHECK ПОЗИЦИЯ ЗАКРЫТА`

### `bot/handlers/admin.py`
- `is_admin`, `admin_stats` (`Users`/`Paper positions`), `admin_ban/unban` (`is_banned`), `admin_active_competition`, `admin_create_demo_cup/seed_demo_players` (`DEMO_SEED_ENABLED`), `admin_reconcile` (`build_leaderboard`), `admin_finish_competition` (`finalize_competition_session` + `notify`), `admin_product_stats` (metrics) — все с `from_user is None or not is_admin` guard, `TG_CHECK/WARNING/CHART/SIREN`

---

## 9. Workers

### `workers/lock.py`
- `LOCK_KEY=82463518`, `acquire_advisory_lock(engine, key, owner)` — `pg_try_advisory_lock` (connection-scoped, не закрывать до `release`), `release_advisory_lock`

### `workers/price_poller.py` (153 строки)
- `_max_leverage_for_symbol` (BTC/ETH 300, SOL 100, остальные 50), `sync_instruments(engine)` — `load_markets` swap, `normalize_symbol`, `USDT` only, `price_precision/quantity_precision/min_quantity` из `market['precision'/'limits']`, `max_leverage` upsert (`Instrument`) с `begin_nested` per symbol, `logger.warning` на скип
- `fetch_once(exchange, engine)` — `fetch_tickers` retry 3× backoff 0.5/1/2с, `PriceSnapshot` per ticker, `validate_snapshot`, `update_snapshot` (local cache), `persist_snapshot` в `market_snapshots` batch `async with factory() as session: for ... await persist ...; await commit`, `consecutive_failures/last_alert_at` (5)
- `poll_prices(engine)` — `swap`/`perpetual`, `while True: fetch_once; sleep(price_poll_interval_seconds)`
- `run_forever(engine)` — `sync_instruments` + `poll_prices`

### `workers/tp_sl_engine.py` (118 строк)
- `check_and_close_positions(engine)` — `SELECT PaperPosition OPEN`, per position `get_execution_snapshot` (stale→`stale_price_rejected`, unavailable→`bingx_error`), `close_price = bid LONG / ask SHORT`, `current_price/unrealized_pnl = calc_unrealized`, `refresh_account_stats`, `reason` TP/SL (`LONG TP>=take_profit` etc.), `begin_nested()` → `close_position(..., tp_sl:{id}:{ts}:{reason})`, `increment tp/sl_triggered`
- `run_forever` — `while True: check_and_close_positions; sleep 1` + `CancelledError`/`logger.exception`

### `workers/competition_lifecycle.py` (107 строк)
- `finalize_competition_session(session, competition_id)` — `SELECT ... FOR UPDATE`, `SELECT PAPER_POSITION OPEN WHERE competition_id`, per `close_position(..., competition_end:{comp}:{pos})` (PaperError → refresh + skip если уже CLOSED), `finish_competition`
- `finalize_expired_competitions(engine)` — `SELECT Competition ACTIVE ends_at<=now FOR UPDATE`, per competition `begin_nested()` → `finalize_competition_session` + `commit` на успех, `notify_competition_finished` после коммита
- `run_forever` — `while True: finalize_expired_competitions; sleep 10`

---

## 10. Поток market-data → торговля

```
ccxt.bingx.fetch_tickers (swap) ──2с──► price_poller.fetch_once
                                         ├─ validate_snapshot (bid/ask/last >0, ask>=bid, exchange_timestamp не в будущем)
                                         ├─ update_snapshot (_price_cache, 3 ключа)
                                         └─ persist_snapshot → market_snapshots (Postgres, batch commit)
                                                               │
bot get_display_snapshot ──► get_shared_snapshot (2000→10000ms) ──► UI цена
services get_execution_snapshot ──► get_shared_snapshot (market_data_max_age_ms) ──► open/close/tp_sl
                                         └─ sqlite fallback _price_cache (тесты)
```

`market_snapshots` — единственный источник истины в проде, локальный `_price_cache` только для `sqlite` тестов.

---

## 11. Торговые инварианты

- `Decimal` везде, `float` нигде в деньгах/PnL
- Баланс — ledger `SUM(account_ledger)`, не поле; `cash_balance/margin_used/equity` — материализованный кэш, `refresh_account_stats`
- `SELECT ... FOR UPDATE` на `trading_accounts` (`_lock_account`)
- `idempotency_key UNIQUE` на `paper_orders`/`account_ledger` + `begin_nested()` savepoint → `same key = same result`, двойной тап не создаёт 2 позиции (`tg:{callback.id}`, `in_flight`)
- Исполнение только по серверной цене в момент обработки (`get_execution_snapshot`), отказ `Market data stale/unavailable`
- `UNIQUE(competition_id,user_id)` + `advisory_xact_lock` → один `CompetitionParticipant` на кубок
- `CHECK balance_after>=0`, `quantity>0`
- `TP/SL` валидация `_validate_tp_sl` + `tp_sl_engine` bid/ask

---

## 12. Premium эмодзи

- `bot/emojis.py` — все ID из ТЗ, `tg_emoji(id, fallback)` → `<tg-emoji emoji-id="...">...</tg-emoji>`
- **LONG `5449683594425410231` / SHORT `5447183459602669338`** — обязательно в `side_keyboard` и сообщениях
- Inline кнопки — `InlineKeyboardButton(text, callback_data/url, icon_custom_emoji_id="...")` (не `🚀` в `text`)
- Reply кнопки — `KeyboardButton(text, request_contact, icon_custom_emoji_id="...")` (`main_menu`, `contact_keyboard`)
- Сообщения — HTML `TG_LONG/TG_SHORT/TG_WARNING/...` (`ParseMode.HTML`, `DefaultBotProperties` в `bot/main.py:37`), `F.text == "Личный кабинет"` (plain, без эмодзи в `text`)

---

## 13. Тесты

- `pytest.ini: asyncio_mode = auto`
- `tests/conftest.py` — `sqlite_engine` (`:memory:`) + `pg_engine` (`TEST_DATABASE_URL`→testcontainers `postgres:15-alpine`, `drop_all/create_all`), `clear_local_market_cache`
- `test_paper_mvp.py` — bid/ask, close retry idempotency, expired competition, validate
- `test_paper_money.py` — leverage `budget*leverage` margin (`500→5x→2500 notional, margin 500`), insufficient, invalid leverage, idempotent grant
- `test_shared_market.py` — shared snapshot source of truth vs local cache, stale/future, finalize skip closed
- `test_demo_acceptance.py` — LONG ASK / SHORT BID, prizes, noop finalize
- `test_paper_race_pg.py` — PG race same key→1 position, one close, one snapshot (skip без Docker)
- `test_product_acceptance.py` — `main_menu` premium icons (`CHART_UP_ID`/`CROWN_ID`), `safe_trade_error` HTML (`WARNING_ID`), `normalize_ticker`, `bingx_chart_url`, `LONG/SHORT IDs`
- `test_tp_sl_leverage_price.py` (10) — `format_side` enum, `fmt_price` low-price (`0.14→$0.140000`, `0.000008→$0.00000800`, `precision=5`), per-coin leverage (`BTC 300 OK` / `UB 300→Max leverage`), global cap 300, LONG/SHORT TP/SL триггер и `not trigger`, `UB` PnL видимость с `price_precision=5`
- Итого `33 passed, 4 skipped` (PG без Docker)

---

## 14. Деплой

- **Railway** (`railway.json` нет, используется `Procfile`+`nixpacks.toml`): `alembic upgrade head && python -m bot.main` (`Railpack` читает `Procfile`, builder `RAILPACK`)
- `nixpacks.toml` — `python311`, `pip install -r requirements.txt`, `start: alembic upgrade head && python -m bot.main`
- `Procfile` — `web: PYTHONPATH=/app alembic upgrade head && PYTHONPATH=/app python -m bot.main`
- `alembic/env.py` — `DATABASE_URL` → `postgresql+asyncpg`, `target_metadata = Base.metadata`
- `requirements.txt` — `aiogram, SQLAlchemy, asyncpg, aiosqlite, alembic, ccxt 4.3.89, pydantic, httpx` (без `fastapi/uvicorn/APScheduler`)
- `runtime.txt` — `python-3.11.11`
- Env прод: `BOT_TOKEN=861...`, `DATABASE_URL=postgresql://postgres:...@postgres.railway.internal:5432/railway`, `MARKET_DATA_MAX_AGE_MS=10000`, `ADMIN_TELEGRAM_IDS`
- Деплой: `railway up --detach -m "..."` → `railway deployment list` → `SUCCESS`, `railway logs` → `Single-process bot starting...`, `Instruments sync complete`, `Run polling for bot @crypto_demo_vbot`
- SSH: `railway ssh --service Postgres -- psql` → `market_snapshots 951`, `BTC/ETH/SOL age 2-4с`; `railway ssh --service CRYPTO_BOT -- python3 /tmp/check.py` → handler checks

---

## 15. Операции и админка

- `admin_stats` — `Users`/`Paper positions`
- `admin_ban/unban <telegram_id> [причина]`
- `admin_active_competition` — `Competition` info
- `admin_create_demo_cup` / `admin_seed_demo_players` — `DEMO_SEED_ENABLED`
- `admin_reconcile` — `build_leaderboard`
- `admin_finish_competition` — ручной `finalize_competition_session` + `notify`
- `admin_product_stats` — `metrics_snapshot` (`trade_opened/closed`, `bingx_error`, `tp/sl_triggered`)
- `workers/lock.py` — advisory lock, `bot/main.py:40` ретрай `15×2с` на rolling deploy

---

## 16. Известные фиксы (аудит)

- `006_widen_symbols` — `VARCHAR(20)→40` для `NCSINASDAQ1002USDUSDT` (22), `price_poller` batch commit вместо per-symbol (951 commit→4с lag)
- `007_instrument_max_leverage` — per-coin (BTC/ETH 300)
- `profile.py:258` — `callback.message.from_user` → `callback.from_user.id` (`_send_profile/_send_transactions` helper) — фікс «Мои сделки» не срабатывает
- `profile.py:9` — добавлены `User/TradingAccount` импорты (`NameError TG_MONEY`)
- `bot/main.py:46` — `db_middleware` `try/rollback`, `lock` retry
- `services/competition.py:45` / `tp_sl_engine:74` / `competition_lifecycle:72` — `begin_nested()` savepoint вместо `rollback()` всего батча
- `bot/views.py:46` — `get_display_snapshot` использует `settings.market_data_max_age_ms` (не хардкод 2000)
- `bot/handlers/trade.py:49` — `LEVERAGES` `1..300` в 2 ряда, `format_side`/`fmt_price` для `PositionSide.LONG` и `$0.14→$0.140000`

---

## 17. Что дальше (не в scope)

- WebSocket вместо REST `fetch_tickers`
- Лимитные/стоп-ордера
- Вывод призов (ручной), P2P переводы
- Более сильный антифрод (`device fingerprint`)

---

*Файл сгенерирован из кода `main` на 2026-08-28. Все пути указаны как в репо `C:\TGOD\CryptoBot`.*
