# CryptoBot — Полная документация проекта (от А до Я)

> **Telegram paper-trading бот** на реальных ценах BingX USDⓈ-M Perpetual.
> **Бот:** `@crypto_demo_vbot` (id `8610467759`)
> **Ветка:** `main` · **Railway:** проект `dynamic-curiosity`, сервисы `CRYPTO_BOT` + `Postgres`
> **Версия документа:** актуальна на коммит `925d36b` (Phase 1 critical fixes задеплоены, деплой `bc976ae9`)

---

## 1. Что это

Демо-тренажёр криптотрейдинга: пользователь получает **$10 000 виртуального баланса**, торгует криптопарами **LONG/SHORT с плечом до 300x** по живым ценам BingX, участвует в недельных турнирах, смотрит топ-10 лидеров. Все деньги виртуальные, цены — реальные.

Ключевые свойства:
- **Один процесс** — бот, воркеры и healthcheck живут в одном event loop
- **Сервер-авторитарность** — цены, PnL, equity, ROI считает сервер, клиент ничего не присылает
- **Ledger-бухгалтерия** — каждая мутация баланса имеет запись в `account_ledger`
- **Идемпотентность** — повторный клик/ретрай не создаёт вторую операцию
- **Изолированная маржа** — убыток не может превысить маржу позиции
- **PostgreSQL-only** для денег (SQLite только для локальных тестов)

---

## 2. Стек

| Слой | Технология | Версия |
|---|---|---|
| Язык | Python | 3.11 |
| Бот | aiogram | 3.22.0 |
| БД | PostgreSQL + SQLAlchemy 2.0 async | 2.0.51 / asyncpg 0.30 |
| Миграции | Alembic | 1.15.2 (rev `009`) |
| Биржа | ccxt (`ccxt.bingx`, swap) | 4.3.89 |
| Конфиг | pydantic-settings | 2.10.1 |
| Healthcheck | aiohttp | 3.9.5 |
| Метрики | file-persisted Counter | встроенно |
| Тесты | pytest + pytest-asyncio | 9.1.1 |
| Деплой | Railway (Railpack) + Procfile | — |

---

## 3. Архитектура — один процесс

```
                        Telegram (long polling)
                                │
                    ┌───────────▼────────────┐
                    │   bot/main.py          │
                    │   python -m bot.main   │
                    │                        │
                    │  Dispatcher (aiogram)  │
                    │   ├─ profile_router    │  /start /profile /сделки /история
                    │   ├─ leaderboard_router│  /top /позиции (+ nav:top/transactions)
                    │   ├─ trade_router      │  /trade → мастер сделки, close, edit TP/SL
                    │   └─ admin_router      │  /admin_* (9 команд)
                    │                        │
                    │  db_middleware         │  session на каждый апдейт, rollback при ошибке
                    │  ThrottlingMiddleware  │  0.8с/сообщение, 0.3с/callback на юзера
                    │                        │
                    │  ФОНОВЫЕ ЗАДАЧИ        │  (asyncio.create_task, тот же loop)
                    │   ├─ price_poller      │  BingX → market_snapshots (каждые 2с)
                    │   ├─ tp_sl_engine      │  mark/LIQ/TP/SL (каждые 1с)
                    │   ├─ competition_lifecycle │  finalize (каждые 10с)
                    │   └─ healthcheck       │  aiohttp :8080 /health /metrics
                    └───────────┬────────────┘
                                │
              PostgreSQL (advisory lock 82463518 — singleton)
              postgres.railway.internal:5432/railway (+ test_railway для тестов)
```

**Почему один процесс:** предыдущая итерация имела раздельные воркеры, и часть кода «не была реально задеплоена». Сейчас всё в одном процессе — теряться негде. Singleton через `pg_try_advisory_lock(82463518)` с ретраем 15×2с при rolling-деплое.

**Healthcheck:** `GET /health` → `200 {"status":"ok","bot":"CRYPTO_BOT"}`; `GET /metrics` → счётчики (JSON). Порт `$PORT` (по умолчанию 8080).

---

## 4. Структура репозитория

```
bot/
  main.py               # входная точка: Bot, Dispatcher, фоновые задачи, healthcheck
  emojis.py             # premium emoji ID (LONG/SHORT/...) + tg_emoji() helper
  views.py              # btn(), fmt_money/fmt_price/fmt_pct/format_side, main_menu, safe_edit
  keyboards.py          # contact_keyboard (request_contact)
  middlewares/throttling.py  # rate-limit per user
  handlers/
    profile.py          # /start, /profile, /сделки (активные), /история (все+статистика)
    trade.py            # /trade: мастер сделки, подтверждение, закрытие, редактор TP/SL
    leaderboard.py      # /top (таблица лидеров), /positions (открытые позиции)
    admin.py            # 9 админ-команд
services/
  paper_adapter.py      # ЯДРО: open_position, close_position, update_position_tp_sl
  bingx_market_data.py  # PriceSnapshot, validate, persist, get_execution_snapshot
  trading_account.py    # счёт, refresh_account_stats (equity/ROI)
  competition.py        # турниры: create/join/equity/finish (clean-sheet reset)
  leaderboard.py        # build_leaderboard (3 SQL-запроса, read-only)
  accounts.py           # get_or_create_user, verify_phone, ensure_can_trade
  pnl.py                # calc_pnl, calc_unrealized, calc_notional
  demo.py               # DEMO_PRIZES, create_demo_cup, seed_demo_players
  notifications.py      # уведомление об итогах турнира
  metrics.py            # счётчики (in-memory + file persist, debounce 10с)
workers/
  price_poller.py       # синк инструментов + батч-поллинг цен
  tp_sl_engine.py       # mark-to-market + liquidation + TP/SL (пагинация 500)
  competition_lifecycle.py  # финализация истёкших турниров
  lock.py               # advisory lock
db/
  models.py             # User
  paper_models.py       # TradingAccount, Instrument, AccountLedger, PaperOrder, PaperPosition, AuditLog
  competition_models.py # Competition, Participant, Execution, Snapshot, Prize
  market_data.py        # MarketSnapshot
alembic/versions/       # 001…009
tests/                  # 245 тестов (все passed)
docs/ANTI_CHEAT_AUDIT.md, AUDIT_DEEP_REPORT.md, AUDIT_REPORT_V2.md
Procfile, nixpacks.toml, runtime.txt, pytest.ini
```

---

## 5. Конфигурация (env)

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `BOT_TOKEN` | — | токен бота (обязателен) |
| `DATABASE_URL` | sqlite | `postgresql://…` → конвертируется в `+asyncpg` |
| `REQUIRE_POSTGRES` | false | fail-fast, если не Postgres |
| `INITIAL_BALANCE_USD` | 10000 | демо-грант при /start |
| `PAPER_SLIPPAGE_BPS` | 0 | проскальзывание (0 = по рынку) |
| `BINGX_MARKET_TYPE` | perpetual | swap |
| `MARKET_DATA_MAX_AGE_MS` | 6000 | порог устаревания цены |
| `PRICE_POLL_INTERVAL_SECONDS` | 2 | интервал опроса |
| `ADMIN_TELEGRAM_IDS` | — | admin через запятую |
| `DEMO_SEED_ENABLED` | false | включение демо-команд |
| `DEMO_PLAYER_COUNT/CUP_DURATION_HOURS/PRIZE_POOL` | 20/24/100 | параметры демо-кубка |
| `METRICS_PATH` / `METRICS_PERSIST_INTERVAL` | metrics.json / 10 | персист метрик |

Railway переменные: `BOT_TOKEN`, `DATABASE_URL` (internal), `MARKET_DATA_MAX_AGE_MS=6000`, `PYTHONPATH=/app`.

---

## 6. База данных

### users
`id`, `telegram_id` (UNIQUE), `username`, `phone_number` (UNIQUE), `phone_verified_at`, `is_banned`, `is_simulated`, `ban_reason`, `created_at`

### trading_accounts (1:1 к юзеру)
| Поле | Тип | Смысл |
|---|---|---|
| `initial_balance` | NUMERIC(18,2) | 10000 — стартовый грант |
| `cash_balance` | NUMERIC(18,2) | свободные деньги |
| `margin_used` | NUMERIC(18,2) | зарезервировано под открытые позиции |
| `equity` | NUMERIC(18,2) | cash + margin_used + unrealized_pnl |
| `available_margin` | NUMERIC(18,2) | = cash_balance |
| `realized_pnl` / `unrealized_pnl` / `total_pnl` | NUMERIC(18,2) | PnL |

### account_ledger (журнал,append-only)
`id`, `account_id`, `type` (`INITIAL_BALANCE|TRADE_OPEN|TRADE_CLOSE|FEE|ADJUSTMENT`), `amount`, `balance_after`, `reference_type`, `reference_id`, `idempotency_key` (UNIQUE), `created_at`

**Инвариант:** `SUM(amount) == cash_balance` — проверяется тестами.
**CHECK:** `balance_after >= 0`.

### instruments
`symbol` (PK, VARCHAR(40)), `base_asset`, `quote_asset`, `status`, `price_precision`, `quantity_precision`, `min_quantity`, `max_quantity`, `max_leverage` (INT; BTC/ETH=300, SOL=100, остальные 50)

### paper_positions
`id`, `account_id`, `competition_id`, `symbol`, `side` (`LONG|SHORT`), `status` (`OPEN|CLOSED|…`), `quantity`, `entry_price`, `current_price`, `notional`, `leverage` (NUMERIC(10,2)), `take_profit`, `stop_loss`, `realized_pnl`, `unrealized_pnl`, `fee_open/close`, `opened_at`, `closed_at`

### paper_orders
`id`, `account_id`, `position_id`, `symbol`, `side`, `quantity`, `requested_price`, `executed_price`, `status` (`PENDING|FILLED|REJECTED`), `reduce_only`, `idempotency_key` (**UNIQUE**), `rejection_reason`

### market_snapshots
`symbol` (PK), `source` (BINGX), `market_type` (PERPETUAL), `bid`, `ask`, `last`, `exchange_timestamp`, `received_at`. CHECK: `bid>0`, `ask>=bid`.

### competitions / competition_participants / executions / competition_leaderboard_snapshots / competition_prizes / audit_logs
Турниры, участники (`UNIQUE(comp,user)`, `starting_equity`, `current_equity`, `roi`, `rank`), иммутабельные исполнения (`OPEN|MANUAL_CLOSE|TAKE_PROFIT|STOP_LOSS|LIQUIDATION`), снапшоты (`UNIQUE(comp,user)`), призы (`UNIQUE(comp,rank)`).

### Миграции
`001` legacy → `002` paper → `003` competition → `004` snapshots/prizes → `005` leverage → `006` VARCHAR(40) symbols → `007` max_leverage → `008` LIQUIDATION enum → `009` индексы. Голова: **009**.

---

## 7. ДЕНЕЖНЫЕ ФОРМУЛЫ (все)

Обозначения: `E` — цена входа (entry), `X` — цена выхода, `Q` — количество, `N` — notional, `M` — маржа, `L` — плечо, `B` — бюджет (маржа, вводит юзер).

### 7.1 Открытие позиции
```
N = B × L                          # объём позиции (notional)
Q = N / E                          # количество монеты
M = N / L = B                      # маржа = бюджет
cash_balance -= M                  # резервирование
margin_used   += M
```
Пример: B=100, L=10, E=100.10 (ASK для LONG) → N=1000, Q=9.99, M=100, cash 10000→9900.

### 7.2 Правила исполнения (спред всегда против трейдера)
| Направление | OPEN | CLOSE |
|---|---|---|
| LONG | **ASK** | **BID** |
| SHORT | **BID** | **ASK** |

### 7.3 PnL
```
LONG:  gross = (X − E) × Q
SHORT: gross = (E − X) × Q
net = gross − fee_open − fee_close      # fees = 0 в демо
```
Пример: LONG E=100.10, X=101.00 (BID), Q=9.99 → gross = 0.90×9.99 = +8.99.

### 7.4 Закрытие
```
M_return = N / L                        # возврат маржи
return_amount = M_return + net          # что возвращается в cash
cash_balance += return_amount
margin_used  -= M_return
realized_pnl += net
```
Пример: M=100, net=+8.99 → return=108.99, cash 9900→10008.99.

### 7.5 Изолированная маржа и гэп (FIX #2)
Если `return_amount < 0` (гэп глубже ликвидации):
```
net = −M                                # убыток ограничен маржой
return_amount = 0                       # ничего не возвращается
ADJUSTMENT-запись: amount=0, reference_type='liquidation_gap',
                   reference_id='{position_id}:gap={|return_amount|}'
```
Деньги не создаются и не исчезают: `SUM(ledger) == cash_balance` — всегда. Гэп поглощается «биржей» и полностью аудируется (reference_id + метрика `liquidation_gap_capped`).

### 7.6 Equity и ROI
```
equity = cash_balance + margin_used + unrealized_pnl
ROI    = (equity − starting_equity) / starting_equity × 100
```
`starting_equity` = 10000 на каждый турнир (clean-sheet: при входе в новый кубок счёт сбрасывается через `ADJUSTMENT`-ledger, открытые позиции прошлого кубка принудительно закрываются).

### 7.7 Ликвидация (tp_sl_engine)
```
margin = N / L
если unrealized_pnl ≤ −margin × 0.9  →  LIQUIDATION (форс-закрытие)
```
Буфер 10%: позиция закрывается раньше, чем убыток превысит маржу — защита от отрицательного баланса. Приоритет: LIQUIDATION > TP/SL.

### 7.8 Unrealized PnL (mark-to-market)
```
LONG:  unrealized = (bid − E) × Q       # закрывался бы по BID
SHORT: unrealized = (E − ask) × Q       # закрывался бы по ASK
```
Пересчитывается каждую секунду движком + при каждом открытии/закрытии.

---

## 8. СИСТЕМА TP/SL (profit-based)

### 8.1 Семантика процентов
**Процент — это доля ПРИБЫЛИ от маржи, а не движение цены.**
```
100% = прибыль равна марже (PnL == margin)

LONG TP = E × (1 + pct / (100 × L))
LONG SL = E × (1 − pct / (100 × L))
SHORT TP = E × (1 − pct / (100 × L))
SHORT SL = E × (1 + pct / (100 × L))
```

### 8.2 Примеры (обязательные)
| Параметры | Расчёт | Результат |
|---|---|---|
| LONG, E=50000, 20%, L=10 | 50000×(1+20/1000) | **TP 51000** |
| LONG, E=50000, 20%, L=10 | 50000×(1−20/1000) | **SL 49000** |
| SHORT, E=50000, 20%, L=10 | 50000×(1−20/1000) | **TP 49000** |
| SHORT, E=50000, 20%, L=10 | 50000×(1+20/1000) | **SL 51000** |
| LONG, E=100, 20%, L=10 | 100×1.02 | TP 102, SL 98 |
| LONG, E=100, 100%, L=1 | 100×2 | TP 200 (прибыль = маржа ×1) |
| LONG, E=100, 100%, L=10 | 100×1.1 | TP 110 |

**НЕЛЬЗЯ путать:** 100% при L=1 — это ×2 к цене, но прибыль ровно 100% маржи. 100% при L=10 — цена ×1.1, прибыль 100% маржи.

### 8.3 Ввод
- **Знак игнорируется** (магнитуда): `20` = `20%` = `-20` = `-20%` = `+20`
- **Режимы:** «Точной ценой» (абсолютные цены: `180 160`) и «В процентах» (`5 -3`, `5% -3%` — знак игнорируется)
- **Одиночные:** `Только TP` / `Только SL` (второй уровень сохраняется прежним)
- **`skip`** — убрать оба уровня
- **Zero отклоняется** (`0`, `0%`, `0 5`) — TP==entry невалиден
- **Malformed** отклоняется (`abc`, `5,5 3,2` — запятая-разделитель → ошибка, `\n`-инъекции невозможны)
- **Плечо `≤0`** отклоняется до деления (защита от DivisionByZero)
- **Оба уровня** — два числа: первое TP, второе SL
- Валидация сервером: `_validate_tp_sl` — LONG: TP>E, SL<E; SHORT: TP<E, SL>E; finite

### 8.4 Редактор TP/SL (открытые позиции)
Кнопка `TP/SL` у каждой открытой позиции → экран редактора с текущими уровнями → те же режимы (цена/процент, только TP/SL, убрать) → `update_position_tp_sl` с серверной проверкой владения, статуса OPEN и активного турнира.

### 8.5 Исполнение TP/SL (tp_sl_engine, каждые 1с)
```
close_price = BID (LONG) / ASK (SHORT)   # серверный снапшот
если unrealized ≤ −margin×0.9            → LIQUIDATION (приоритет)
иначе если price ≥ TP (LONG) / ≤ TP (SHORT) → TP
иначе если price ≤ SL (LONG) / ≥ SL (SHORT) → SL
```
Каждая страница позиций (500) — отдельная сессия+commit; close внутри `begin_nested`; liquidation/TP/SL — отдельные `ExecutionReason`.

---

## 9. ИСПОЛНЕНИЕ И ИДЕМПОТЕНТНОСТЬ

### 9.1 Порядок open_position (атомарно)
```
1. валидации (сторона, плечо 1..300 + max_leverage инструмента, TP/SL)
2. snapshot (stale → отказ «Рынок недоступен»)
3. цена = ASK/BID по стороне
4. quantity = notional / цена, min/max checks
5. SELECT FOR UPDATE на trading_accounts
6. re-check idempotency ПОСЛЕ lock (видит коммит конкурента)
7. margin check (required ≤ available) → иначе REJECTED-order
8. begin_nested: PaperOrder FILLED + PaperPosition + Execution
                  + cash−margin + ledger + refresh_stats   ← ВСЁ В ОДНОМ SAVEPOINT
9. IntegrityError → savepoint rollback → _resolve_idempotent_position
10. (в handler) commit
```

### 9.2 Порядок close_position (атомарно)
```
1. pre-check idempotency key / статус OPEN
2. snapshot → close_price = BID/ASK
3. SELECT FOR UPDATE на trading_accounts
4. refresh → повторный статус-чек (защита от double-close с ЛЮБЫМИ ключами)
5. begin_nested: PaperOrder reduce_only + position CLOSED + Execution
                 + gap-cap + cash+return + ledger TRADE_CLOSE (+ADJUSTMENT)
                 + refresh_stats                              ← ОДИН SAVEPOINT
6. IntegrityError → rollback savepoint → идемпотентный ответ
```

### 9.3 Ключи идемпотентности
| Операция | Ключ | Защита |
|---|---|---|
| OPEN | `tg:{callback.id}` | UNIQUE paper_orders + re-check после lock |
| CLOSE (manual) | `tg_close:{callback.id}` | UNIQUE + статус-гейт до и после lock |
| CLOSE (TP/SL/LIQ) | `tp_sl:{pos}:{ts}:{reason}` | UNIQUE + статус-гейт |
| Finalize close | `competition_end:{comp}:{pos}` | UNIQUE + `FOR UPDATE` на competitions |
| Ledger open | `{key}:ledger` | UNIQUE account_ledger |
| Ledger close | `{key}:ledger` | UNIQUE |
| Gap ADJUSTMENT | `{key}:gap` | UNIQUE |
| Cup reset | `reset:{acc}:{comp}` | UNIQUE |

**Гарантия:** один ключ → максимум одна финансовая операция, атомарно (ledger+account+order+position в одном savepoint). Повтор → тот же результат без повторной мутации.

### 9.4 Гонки (проверено на реальном PG)
| Гонка | Защита | Тест (PG, пройден) |
|---|---|---|
| Double OPEN (same key) | FOR UPDATE + UNIQUE + resolve | `test_pg_concurrent_open_same_key_single_effect` |
| Double CLOSE (same key) | FOR UPDATE + статус-гейт + UNIQUE | `test_pg_concurrent_close_same_key_single_effect` |
| Manual close vs TP/SL | account FOR UPDATE сериализует; статус-гейт после lock | `test_manual_close_race_has_one_close` |
| Finalize vs trade/close | FOR UPDATE на competitions + savepoint | `test_two_finalizers_create_one_snapshot` |
| Full cycle atomicity | reconciliation после open и close | `test_pg_open_close_ledger_account_atomic` |

---

## 10. MARKET DATA

```
BingX (ccxt.fetch_tickers, swap, только DEMO_WATCHLIST 25 пар + открытые позиции)
  → PriceSnapshot (bid/ask/last, exchange_timestamp)
  → validate: bid>0, ask>=bid, finite, timestamp не из будущего (>5с)
  → market_snapshots (PostgreSQL, UPSERT, батч-commit)
       ↓
get_execution_snapshot(session, symbol, max_age=6000мс)
  → stale/unavailable → PaperError → «Рынок временно недоступен»
```
- **Локальный кэш** — только fallback на SQLite (тесты). На PG исполнение всегда из БД.
- Retry: 3 попытки с backoff 0.5/1/2с; `ALERT` после 5 подряд неудач.
- instruments sync при старте: цена/кол-во precision, min_quantity из BingX; max_leverage только вверх.

---

## 11. TELEGRAM-ИНТЕРФЕЙС

### Команды (все с ignore_case, русские алиасы)
| Команда | Описание |
|---|---|
| `/start` | регистрация, верификация номера, грант |
| `/profile` `/профиль` | личный кабинет: юзернейм, баланс, счётчики, ROE, место |
| `/sdelki` `/сделки` `/активные` | **активные** сделки (пагинация 5/стр) |
| `/history` `/история` `/все_сделки` | закрытые сделки + статистика (win-rate, +/−, лучшая/худшая) |
| `/trade` `/торговать` | меню торговли |
| `/top` `/топ` `/лидеры` | топ-10 с пагинацией |
| `/positions` `/позиции` | открытые позиции |
| `/admin_*` (9) | только для `ADMIN_TELEGRAM_IDS` |

### Reply-меню (главное)
`Торговать` · `Личный кабинет` | `Топ 10` · `Сделки` — все с premium-иконками.

### Кнопки (inline) — цвет + premium-иконка
Bot API 9.4 `style`: `danger` (красный) / `success` (зелёный) / `primary` (синий) **вместе с** `icon_custom_emoji_id`.

| Цвет | Кнопки |
|---|---|
| `danger` 🔴 | Закрыть {COIN} {SIDE}, Да закрыть, Отмена, Убрать TP/SL, SHORT |
| `success` 🟢 | Обновить, Подтвердить сделку, LONG, Торговать, Только TP |
| `primary` 🔵 | Посмотреть все сделки, Топ 10, Топ, Активные сделки, Установить TP/SL, Выбрать монету, Быстрое открытие, Мои сделки, плечи 1x–300x |
| default | Назад, пагинация ◀ ▶, Пропустить |

Helper: `btn(text, callback_data, icon=…, style=…)` в `bot/views.py`.

### Premium emoji (обязательные ID)
- **LONG** `5449683594425410231` · **SHORT** `5447183459602669338`
- Полный список — `bot/emojis.py`; в сообщениях — `<tg-emoji emoji-id="…">fallback</tg-emoji>`, в кнопках — `icon_custom_emoji_id`, обычные эмодзи в кнопках запрещены.

### Поток сделки (мастер)
```
/trade → [1. Выбрать монету] тикер → график BingX (ссылка) + кнопка «Открыть сделку»
       → [2. Быстрое открытие] тикер → бюджет (USD, маржа) → плечо (фильтр по max_leverage)
       → LONG/SHORT → TP/SL (цена/проценты/одно/пропустить) → подтверждение (цена входа ASK/BID)
       → trade:confirm → open_position (idempotent) → «ПОЗИЦИЯ ОТКРЫТА»
```
Состояние — `trade_state: dict[uid]`, очищается при навигации; `SkipHandler` не перехватывает команды/меню; `in_flight` + `tg:{callback.id}` против двойного клика.

---

## 12. SECURITY

| Угроза | Защита |
|---|---|
| SQL injection | все запросы — SQLAlchemy ORM / bound `text(:param)`; f-string SQL = 0 |
| XSS | `html.escape(username)` в profile/leaderboard; Telegram HTML-режим |
| IDOR (close) | `position.account_id == account.id` до и после lock |
| IDOR (edit TP/SL) | `_get_owned_open_position` = `WHERE id AND account_id`; серверный assert в `update_position_tp_sl` |
| IDOR (reads) | все выборки `WHERE account_id == own account` |
| Подмена цены/PnL/equity | клиент присылает только тикер/бюджет/плечо/TP-SL; всё остальное — сервер |
| Мультиаккаунт по номеру | `phone_number UNIQUE` + `request_contact` (user_id совпадение) |
| Спам | ThrottlingMiddleware 0.8с/0.3с + in_flight + advisory lock |
| Ban bypass | `ensure_can_trade` на открытии; ban/phone в сервисном слое |
| Секреты | BOT_TOKEN/DATABASE_URL не логируются; `.env` в gitignore; `/metrics` без PII |

**Известные ограничения:** banned-юзер может закрыть/отредактировать позицию (но не открыть); `/health`/`/metrics` без rate-limit (ок для internal).

---

## 13. ТУРНИРЫ И ЛИДЕРБОРД

- Турнир создаётся автоматически (`Weekly Trading Cup #1`, 7 дней, prize 500) либо админом (`DEMO TRADING CUP`, 24ч, prize 100).
- `join_competition` — clean-sheet: закрыть открытые позиции прошлого кубка (idempotent `cup_reset:{acc}:{comp}`), сброс счёта до 10000 через `ADJUSTMENT`, `starting_equity=10000`.
- `build_leaderboard` — 3 SQL-запроса (без N+1), read-only, сортировка ROI↓ → equity↓ → joined_at; `current_equity = cash + margin_used + unrealized`.
- Финализация (`ends_at`): `FOR UPDATE` на competitions, per-competition savepoint, закрытие всех позиций (`competition_end:{comp}:{pos}`), снапшот (1 раз, `UNIQUE(comp,user)`), призы DEMO (1 раз, `UNIQUE(comp,rank)`), уведомления best-effort.
- `/top` — live-таблица с медалями 🥇🥈🥉, пагинация по 10, «Твоё место», таймер до итогов; после финала — снапшот «ФИНАЛ».

---

## 14. ЛИКВИДАЦИЯ

```
Каждые 1с по каждой открытой позиции:
  mark = BID(LONG) / ASK(SHORT) из свежего снапшота
  unrealized = mark-PnL
  margin = notional / leverage
  если unrealized ≤ −margin×0.9 → форс-закрытие reason='LIQUIDATION'
```
- Буфер 10%: закрывается до того, как убыток превысит маржу.
- Изоляция: даже при гэпе убыток юзера ограничен маржой (см. 7.5).
- `ExecutionReason.LIQUIDATION` — отдельная иммутабельная запись.
- Пример: 300x, notional 3000, margin 10 → ликвидация при движении 0.3% против.

---

## 15. ТЕСТЫ

**Локально:** `245 passed, 0 failed` (включая PG через testcontainers/Docker).
**На реальном PG** (`railway ssh` → `test_railway`, изолированная БД): **7 passed** (4 paper_race + 3 Phase 1 concurrency).

| Файл | Покрывает |
|---|---|
| test_phase1_fixes.py (18) | FIX #1 negative %, FIX #2 ADJUSTMENT+reconciliation, FIX #3 idempotency, FIX #4 IDOR |
| test_phase1_concurrency_pg.py (3, pg) | конкурентные open/close same key, атомарность (реальный PG) |
| test_paper_race_pg.py (4, pg) | same-key open, manual close race, finalize race, no-cache-fallback |
| test_liquidation.py (3) | capped loss, engine LIQUIDATION, not premature |
| test_tp_sl_leverage_price.py (10) | TP/SL триггеры, плечо per-coin, fmt_price, format_side |
| test_competition_isolation.py (4) | clean-sheet, несколько позиций |
| test_shared_market.py (8) | канонический снапшот, stale/future, finalize |
| test_paper_money.py (4) | маржа/плечо, insufficient, грант idempotent |
| test_paper_mvp.py (3) | ASK/BID, retry, expired |
| test_demo_acceptance.py (2) | LONG ASK/SHORT BID, призы, noop |
| test_leaderboard.py (3) | таблица, медали, финал |
| test_product_acceptance.py (7) | меню, команды, формулы URL, premium ID |

Запуск PG-тестов: `railway ssh --service CRYPTO_BOT -- "cd /app && pytest tests/test_phase1_concurrency_pg.py tests/test_paper_race_pg.py -v"`.

---

## 16. ДЕПЛОЙ

- `Procfile`: `web: PYTHONPATH=/app alembic upgrade head && PYTHONPATH=/app python -m bot.main`
- Railway Railpack, Python 3.11, 1 реплика, restart ON_FAILURE
- `railway up --detach` — загрузка кода; `railway ssh` — доступ к контейнеру/БД
- Миграции применяются при старте (single replica — без гонок); БД сейчас на `009 (head)`
- Rolling deploy: advisory lock retry 15×2с — старый контейнер освобождает lock, новый стартует
- Проверка: логи `Single-process bot starting…` → `Run polling` → `Instruments sync complete`; `/health` 200

---

## 17. АДМИН-КОМАНДЫ

`/admin_stats` · `/admin_ban <id> <причина>` · `/admin_unban <id>` · `/admin_active_competition` · `/admin_create_demo_cup` · `/admin_seed_demo_players` · `/admin_reconcile` · `/admin_finish_competition` (ручная финализация) · `/admin_product_stats` (метрики) — все с `is_admin` + `DEMO_SEED_ENABLED` гейтингом для seed.

---

## 18. ИСТОРИЯ КРИТИЧЕСКИХ ФИКСОВ

| Fix | Суть | Коммит |
|---|---|---|
| IDOR edit TP/SL | `WHERE id AND account_id` во всех 5 путях + серверный assert | Phase 1 |
| ADJUSTMENT при гэпе | явный ledger при cap, без создания денег | Phase 1 |
| Атомарность | ledger+account внутри savepoint (open+close) | Phase 1 |
| Negative % | знак игнорируется, `val==0` reject | Phase 1 |
| Leaderboard SQL | 3 запроса вместо 2N+1, read-only | audit v2 |
| `Топ 10` hijack | SkipHandler + порядок роутеров | audit v2 |
| tpsl:back перехват | `action=="back"` внутри cb_tp_sl | audit v2 |
| Stale при close | MAX_AGE 6000мс + watchlist 25 пар | до Phase 1 |
| Liquidation | 90% маржи + cap + ADJUSTMENT | P0 |
| Perf | batch persist, пагинация 500, throttling | audit v2 |

---

## 19. ОГРАНИЧЕНИЯ И PHASE 2 (известные, не блокеры)

- Tick-size: TP/SL квантуется к 8 знакам, не к `price_precision` инструмента (может не сработать на границе тика)
- `is_percent` эвристика: `%` в тексте переключает режим даже в «точной цене»
- `FOR UPDATE` только на `trading_accounts`; на `paper_positions` — статус-гейт (достаточно при same-account)
- Leaderboard считает equity на лету — `Participant.current_equity` может отставать между сделками
- WebSocket вместо REST-поллинга; лимитные ордера; выплаты призов — ручные

---

## 20. БЫСТРЫЙ СТАРТ

```bash
# локально
pip install -r requirements.txt
cp .env.example .env          # заполнить BOT_TOKEN, DATABASE_URL
alembic upgrade head
python -m bot.main
pytest -q                     # 245 passed

# Railway
railway up --detach -m "…"
railway logs --service CRYPTO_BOT
railway ssh --service CRYPTO_BOT -- "pytest tests/ -q"   # PG-тесты на test_railway
```

---

*Документ сгенерирован из актуального кода `main` @ `925d36b`. Формулы верифицированы тестами на SQLite и реальном PostgreSQL.*
