# Самоаудит анти-чит таблицы (AGENTS_1.md раздел 6)

| # | Вектор | Защита (код) | Тест (file:line) | Статус |
|---|---|---|---|---|
| 1 | Race condition на параллельных ордерах | `db/repo.py:31` `SELECT FOR UPDATE` на `users.id` + проверка баланса внутри той же транзакции `services/trading.py:107` | `tests/test_race_pg.py:18` реальная гонка на PG с `asyncio.sleep(0.3)` внутри `FOR UPDATE`; smoke на sqlite `tests/test_gaps.py:65` | ✅ PG-тест, sqlite smoke |
| 2 | Двойной тап кнопки / повторная отправка команды | `idempotency_key` UNIQUE на `orders`/`transactions` `db/models.py:93,110` + no-op в `services/trading.py:54` | `tests/test_money.py:15` `test_idempotency_no_double` | ✅ |
| 3 | Устаревшая цена / игра на известном движении | `services/pricing.py:25` `get_price_or_raise` + `MAX_PRICE_STALENESS_SECONDS` + отказ `services/trading.py:62,95,211` | `tests/test_money.py:32` `test_stale_price_rejected`; `tests/test_gaps.py:43` staleness после 5 fails | ✅ |
| 4 | Мультиаккаунтинг ради нескольких призов | `request_contact` + `phone_number UNIQUE` `db/models.py:51` + `services/accounts.py:24` + ручная проверка `bot/handlers/admin.py:22` | Не покрыто автотестом напрямую — защита на уровне схемы (UNIQUE phone) + админ-флоу. Добавлен `tests/test_gaps.py:18` midweek grant использует тот же UNIQUE, косвенно проверяет | ⚠️ схема + ручная проверка |
| 5 | Сговор между аккаунтами (перекидывание баланса) | P2P отсутствует — единственный контрагент рынок (`services/trading.py` только `ccxt` price feed) | Не покрыто тестом — отсутствие кода P2P, доказывается отсутствием таблицы/эндпоинта `grep -r transfer` пусто | ⚠️ гарантия отсутствием фичи |
| 6 | Манипуляция низколиквидными монетами | `is_quote_eligible` + `MIN_24H_QUOTE_VOLUME_USDT` `workers/price_poller.py:66` + учёт только eligible в `services/weekly_cycle.py:95` | `tests/test_money.py:78` `test_non_eligible_not_in_snapshot` | ✅ |
| 7 | Двойное начисление за неделю / повторный запуск крона | `UNIQUE (user_id, week_id) WHERE type='WEEKLY_GRANT'` partial index `alembic/versions/001_initial.py:69` + `services/weekly_cycle.py:30` | `tests/test_money.py:22` `test_double_grant_once`; `tests/test_race_pg.py:95` race grant | ✅ PG-тест |
| 8 | Торговля без верификации | `services/accounts.py:38` `ensure_can_trade` проверяет `phone_verified_at` перед `trading.py:46` | `tests/test_money.py:55` `test_trade_without_phone_rejected` | ✅ |
| 9 | Забаненный пользователь продолжает торговать | `services/accounts.py:39` `is_banned` в том же слое | `tests/test_money.py:68` `test_banned_rejected` | ✅ |

Дополнительно:
- `CHECK balance_after >=0` `db/models.py:90` — `tests/test_money.py:82` + `tests/test_race_pg.py:78`
- `CHECK qty >=0` `db/models.py:123`
- Дедлок-безопасность: одна строка `users` `db/repo.py:31` — `tests/test_race_pg.py:43` + `tests/test_gaps.py:65`
- Mid-week grant — `services/accounts.py:29` — `tests/test_gaps.py:18`
- Price poller resilience — `workers/price_poller.py:32` — `tests/test_gaps.py:43`
- Системный инвариант — `tests/test_gaps.py:85`
