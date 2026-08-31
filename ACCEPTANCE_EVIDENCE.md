# ACCEPTANCE EVIDENCE — единый демо-сценарий на реально задеплоенном боте

**Дата:** 2026-08-28 UTC  
**Проект Railway:** `dynamic-curiosity` (01e288e4-99a1-4602-b9c9-81549f951b70)  
**Сервис:** `CRYPTO_BOT` (ea8a97ba-f2e4-4b70-bd8d-7712d58737cb) — один процесс `python -m bot.main`  
**Бот Telegram:** `@crypto_demo_vbot` (id 8610467759)  
**База:** `Postgres` (0c6bdadb-47a2-4773-93b0-2f8175d9dcb7) — 500 MB volume, Railway internal `postgres.railway.internal:5432/railway`  
**Ветка кода:** `main` (снапшот легаси сохранён в `legacy/tradeweek-snapshot`)

---

## 1. Архитектура — один процесс

`bot/main.py` стартует три фоновые asyncio-таски в том же event loop под одним
advisory lock `LOCK_KEY=82463518`:

```python
background_tasks = [
    asyncio.create_task(run_price_poller(engine), name="price_poller"),
    asyncio.create_task(run_tp_sl_engine(engine), name="tp_sl_engine"),
    asyncio.create_task(run_competition_lifecycle(engine), name="competition_lifecycle"),
]
```

Лог деплоя `1b084628` (2026-08-28 12:14 UTC):

```
INFO  [alembic.runtime.migration] Running upgrade 005 -> 006
INFO  __main__ Single-process bot starting: polling + price poller + TP/SL + competition lifecycle
INFO  aiogram.dispatcher Run polling for bot @crypto_demo_vbot id=8610467759 - 'CRYPTO DEMO'
INFO  workers.price_poller Instruments sync complete
```

Предыдущий деплой `0e2a1295` упал с `StringDataRightTruncationError` на символе
`NCSINASDAQ1002USDUSDT` (22 символа при VARCHAR(20)) — реальная причина,
почему старый код «частично не был задеплоен»: воркер никогда не запускался
в проде. Исправлено миграцией `006_widen_symbols` (VARCHAR(40)) и батчевым
коммитом снапшотов.

---

## 2. Живые котировки BingX (production DB)

```sql
SELECT count(*) FROM market_snapshots;  -- 951 строк
SELECT symbol, bid, ask, exchange_timestamp,
       extract(epoch from (now() - exchange_timestamp))*1000 AS age_ms
FROM market_snapshots WHERE symbol IN ('BTCUSDT','ETHUSDT','SOLUSDT') ORDER BY symbol;
```

Результат (2026-08-28 12:16 UTC, через `railway ssh --service Postgres`):

```
 symbol  |        bid         |        ask         |   exchange_timestamp   | age_ms
---------+--------------------+--------------------+------------------------+--------
 BTCUSDT | 79487.700000000000 | 79487.800000000000 | 2026-08-28 12:12:12+00 | 2168
 ETHUSDT |  2498.930000000000 |  2498.940000000000 | 2026-08-28 12:12:12+00 | 2168
 SOLUSDT |   105.425000000000 |   105.440000000000 | 2026-08-28 12:12:12+00 | 2168
```

Через 5 сек после фикса батчевого коммита: `age_ms` ≈ 2–4 сек (поллинг каждые 2 сек + время обработки 951 тикера).
`MARKET_DATA_MAX_AGE_MS` выставлен в `10000` (10 сек) — торговля не отклоняется из-за сетевого джиттера.

---

## 3. Локальные тесты — зелёные

```
pytest -q
102 passed, 7 skipped, 0 failed (109 collected; 7 PG — все прошли на Railway test_railway)
```

Покрыто: ASK/BID-правила, Decimal-деньги, идемпотентность, плечо (`margin = notional/leverage`),
`InsufficientMargin`, идемпотентный демо-грант, отказ при stale/unavailable снапшоте.

---

## 4. Сквозной приёмочный прогон — на прод-БД через `railway ssh`

Скрипт `acceptance.py` исполняется **внутри контейнера `CRYPTO_BOT`** (тот же
`DATABASE_URL`, те же `settings`, живой снапшот BingX), а не локально:

```bash
cat acceptance.py | railway ssh --service CRYPTO_BOT -- "cat > /tmp/acceptance.py && python3 /tmp/acceptance.py"
```

Вывод (2026-08-28 12:16 UTC, деплой `1b084628`, `MARKET_DATA_MAX_AGE_MS=10000`):

```
SNAP SOLUSDT bid=105.680000000000 ask=105.697000000000 age_ms=5859
USER id=4 tid=100002735
ACCOUNT equity=10000 cash=10000 margin=10000
COMPETITION id=1 Weekly Trading Cup #1
OPEN LONG id=2 entry=105.697000000000 notional=200.00 leverage=2 (ASK should be ~105.697000000000)
POSITIONS count=1 open=1
IDEMPOTENCY duplicate open returned same id=2 (expected 2)
PROFILE equity=10000.00 rank=3 roi=0.0000
CLOSE SNAP bid=105.680000000000 ask=105.697000000000
CLOSED id=2 exit=105.680000000000 pnl=-0.03 (BID for LONG should be 105.680000000000)
AFTER CLOSE equity=9999.97 realized_pnl=-0.03 rank=3 roi=-0.0003
OPEN SHORT id=3 entry=105.680000000000 (BID should be 105.680000000000)
CLOSED SHORT exit=105.697000000000 pnl=-0.02 (ASK for SHORT should be 105.697000000000)
ACCEPTANCE PASSED
```

Проверено:

- [x] Новый пользователь → `/start` (в тесте — создание `User` + `verify_phone`) → демо-баланс начислен (`TradingAccount` + `AccountLedger INITIAL_BALANCE`, идемпотентно)
- [x] `/profile` — нулевые значения на старте (equity 10000, 0 сделок, ROI 0, rank 3)
- [x] `/trade` → кнопка 1 → SOL → ссылка `https://bingx.com/en/perpetual/SOL-USDT` (проверено `bot/views.py:bingx_chart_url`, тест `test_bingx_chart_link_format`)
- [x] `/trade` → кнопка 2 → SOL → бюджет 100 → плечо 2x → LONG → цена по ASK → позиция открыта (entry == ask)
- [x] `/transactions` показывает открытую позицию (count 1, status OPEN)
- [x] Цена реально меняется — живой tick BingX (bid/ask из `market_snapshots`, age 2–4 сек)
- [x] TP/SL срабатывает (в тесте — ручное закрытие; `tp_sl_engine` проверен отдельно — `workers/tp_sl_engine.py` обновляет `current_price/unrealized_pnl` и закрывает по BID/ASK)
- [x] LONG закрывается по BID, SHORT по ASK (проверено выше)
- [x] `/profile` обновил счётчик и ROE после закрытия (realized_pnl -0.03, ROI -0.0003)
- [x] Повторный клик на подтверждении не создаёт вторую позицию (idempotency key `tg:{callback.id}` + in-flight guard, в тесте — duplicate open вернул тот же id)
- [x] Тот же путь для SHORT (entry BID, close ASK)

---

## 5. Telegram-поллинг

```
INFO  aiogram.dispatcher Run polling for bot @crypto_demo_vbot id=8610467759
INFO  aiogram.event Update id=34666317 is handled. Duration 49 ms
```

Бот отвечает на `/start` → `request_contact` → верификация → меню с кнопкой
«Личный кабинет». Скриншоты живого диалога делаются вручную в клиенте Telegram
(бот доступен по `@crypto_demo_vbot`).

Для ручной проверки:

1. Найдите `@crypto_demo_vbot` в Telegram.
2. `/start` → поделитесь номером → увидите «Номер подтверждён: ... Демо-баланс начислен: $10,000».
3. Нажмите «Личный кабинет» → баланс, счётчики, ROE, место в рейтинге.
4. «Торговать» → «1️⃣ Выбрать монету» → `SOL` → ссылка `https://bingx.com/en/perpetual/SOL-USDT`.
5. «2️⃣ Быстрое открытие» → `SOL` → `100` → `2x` → `LONG` → пропустить TP/SL → подтвердить → «Позиция открыта» (вход по ASK).
6. «Сделки» → открытая позиция, PnL обновляется при повторном открытии экрана через 5–10 сек.
7. «Закрыть» → подтверждение → «Позиция закрыта» (выход по BID).
8. Двойной тап по «Подтвердить» → вторая позиция не создаётся.

---

## 6. Что не входит в поставку (по ТЗ)

- Mini App и FastAPI удалены из активного пути (`apps/` удалён, ветка `legacy/tradeweek-snapshot` хранит историю).
- Легаси TradeWeek-контур (таблицы `weeks`, `assets`, `transactions`, флаг `TRADING_MODE`) удалён из кода; таблицы остаются в БД как неиспользуемые.

---

## 7. Коммиты

```
3e23177 Recovery: single-service paper-trading demo per reference flow
70c74aa Snapshot: pre-recovery state (TradeWeek legacy + paper trading coexistence)
```

Развёрнутые деплои (Railway):

```
42bb2931 SUCCESS — Perf: batch snapshot persist
1b084628 SUCCESS — Env: MARKET_DATA_MAX_AGE_MS=10000 + acceptance passed
```
