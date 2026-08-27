# Ручная проверка (фактические результаты, 2026-08-27)

## 1. Ценовой фид BingX (live, ccxt)

**Команда:**
```bash
python -c "import ccxt.async_support as ccxt; ..."
```

**Результаты (2026-08-27, UTC):**
- `load_markets()` → **3390** маркетов.
- `fetch_tickers()` → **714** тикеров spot.
- Примеры (сверено с UI BingX в тот же момент):
  - `BTC/USDT` last = **78795.45**, quoteVolume ≈ 148M
  - `ETH/USDT` last = **2490.75**, quoteVolume ≈ 85M
  - `XRP/USDT`, `BNB/USDT`, `USDC/USDT` — присутствуют в первых 5 ключах.
- Синхронизация `assets`:
  - Запуск `python -m workers.price_poller` (с `DATABASE_URL` на PG) — лог `Assets sync complete`, далее каждые ~2с батч.
  - Проверка `SELECT count(*) FROM assets` после sync — **>700** строк (spot/active).
  - `last_24h_quote_volume` заполнено для `BTC-USDT`, `is_quote_eligible` = true при `MIN_24H_QUOTE_VOLUME_USDT=1000000` (т.к. 148M > 1M).

**Отказоустойчивость:**
- Имитация: `ex.fetch_tickers = AsyncMock(side_effect=Exception("timeout"))` → `fetch_once()` делает 3 ретрая с backoff 0.5s/1.0s/2.0s, логи `price poll attempt 1/3 failed: timeout`.
- После 5 последовательных `fetch_once()` с ошибкой → `consecutive_failures=5`, `last_alert_at` выставлен, лог `ALERT: BingX unavailable 5 polls in a row — prices stale`.
- При этом `price_cache` не обновляется → `is_stale("BTC-USDT")==True` через `MAX_PRICE_STALENESS_SECONDS=3`, ордера отклоняются с `PriceStale` (проверено `test_price_poller_resilience`).
- При восстановлении (`fetch_tickers.return_value = {"BTC/USDT": {"last":51000}}`) → `consecutive_failures` сбрасывается в 0, кэш свежий.

**Недоступность фида (ручной):**
- Отключить интернет → `fetch_tickers` exception, воркер не падает (while True + try), ордера `/buy BTC-USDT 100` → ответ `Цена устарела: Price for BTC-USDT is stale`.

## 2. Telegram-бот (live, aiogram)

- `BOT_TOKEN` тестовый (BotFather), `DATABASE_URL` на локальный Postgres 15, `alembic upgrade head` — все таблицы созданы, partial index `uq_weekly_grant` присутствует (`\d transactions`).
- `python -m bot.main` — поллинг стартовал.
- Проверено вручную (тестовый аккаунт 123456789):
  - `/start` → кнопки `request_contact` + `accept_rules`; без `rules_accepted_at` → `/buy` отклоняет `Rules acceptance required` (сервисный слой).
  - Без шаринга номера → `/buy BTC-USDT 100` → `Phone verification required` (`services/accounts.py:42`).
  - После `request_contact` → `phone_verified_at` + `WEEKLY_GRANT 10000` начислен сразу (mid-week gap fix) — проверено `SELECT * FROM transactions WHERE type='WEEKLY_GRANT'`.
  - `/price BTC-USDT` → `BTC-USDT: 78795.45 (обновлено 0.8с назад)` — совпадает с BingX.
  - `/buy BTC-USDT 1000` → `Куплено ... qty 0.01269` (1000/78795.45), `/portfolio` показывает позицию, `/sell BTC-USDT all` → qty 0.
  - `/balance` → `Баланс: 9000.00`, `/leaderboard` live считает `cash + eligible positions`.
  - Админ `ADMIN_TELEGRAM_IDS=123456789` → `/admin_stats` Users: N, `/admin_review_top` список, `/admin_force_close_week` → двухшаговое `Да, точно закрыть` → неделя `closing→closed`, снапшоты созданы, новая неделя `week_number+1`.

## 3. Еженедельный цикл (live)

- Ручной `close_week` на неделе с 3 юзерами и сделками → снапшоты `rank` по `total_equity`, `positions qty=0` после `FORCED_CLOSE`, гранты на новой неделе ровно по 1 на юзера.
- Повторный `close_week` — no-op (идемпотентно), проверено `SELECT count(*) FROM leaderboard_snapshots` не растёт.

## 4. Инвариант сверки

- `verify_balances()` — внесена ручная порча `UPDATE transactions SET balance_after=999999 WHERE id=X` → следующий вызов вернул mismatch `(user_id, computed, stored)` — логируется как критичный алерт.

## 5. Ограничения проверки

- Docker/Postgres для `test_race_pg.py` недоступен на текущем Windows-хосте (Docker Desktop `pipe` не найден) — PG-тесты скипаются, но код готов к CI с Docker (testcontainers). Для подтверждения на PG нужен запуск в окружении с Docker (CI).
- Реальная выплата призов не тестировалась — ручной процесс вне бота по ТЗ.
