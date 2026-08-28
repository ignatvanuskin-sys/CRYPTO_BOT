# Самоаудит анти-чит (paper trading с плечом, актуально на 2026-08-28)

> Обновлено для leverage-версии (было: spot без плеча по AGENTS_1.md). Архитектура: единый процесс `bot/main.py`, ledger `account_ledger`, ASK/BID, `market_snapshots` + `price_poller`, `tp_sl_engine` + `liquidation`.

| # | Вектор | Защита (код) | Тест (file:line) | Статус |
|---|---|---|---|---|
| 1 | Race condition на параллельных ордерах | `services/paper_adapter.py:216` `SELECT ... FOR UPDATE` на `trading_accounts.id` + проверка `required_margin > available` внутри той же транзакции, `begin_nested()` savepoint на idempotency | `tests/test_paper_race_pg.py:83` `test_same_open_key_is_one_position` (PG, 2 concurrent `asyncio.gather` same key → 1 position) + `test_manual_close_race_has_one_close` | ✅ PG (прошел на Railway test_railway) |
| 2 | Двойной тап / повтор команды | `idempotency_key UNIQUE` на `paper_orders`/`account_ledger` (`paper_models.py:108,92`) + `begin_nested()` в `paper_adapter.py:240` + `in_flight` guard `trade.py:465` | `test_paper_mvp.py:48` `test_bid_ask_execution_and_close_retry` (retry same key), `test_liquidation.py:10` idempotent, `test_tp_sl_leverage_price.py` | ✅ |
| 3 | Устаревшая цена | `services/bingx_market_data.py:30` `validate_snapshot` + `is_stale` + `get_execution_snapshot` с `MARKET_DATA_MAX_AGE_MS=3000` (было 10000, снижено после фильтрации до 25 пар) + отказ `paper_adapter.py:166` | `test_shared_market.py:114` `test_stale_shared_snapshot_rejected`, `test_paper_mvp.py:99` `validate`, `test_liquidation.py` | ✅ |
| 4 | Мультиаккаунтинг | `request_contact` (`bot/keyboards.py:6` `ENVELOPE_ID`) + `phone_number UNIQUE` (`models.py:51`) + `verify_phone` + ручная `admin_ban` | Схема `UNIQUE phone` + `test_competition_isolation.py` (clean sheet) косвенно | ⚠️ схема + ручная |
| 5 | Сговор (перекидывание) | P2P отсутствует — единственный контрагент рынок (BingX `fetch_tickers`), нет таблицы transfer | `grep -r transfer` пусто, отсутствие кода | ⚠️ гарантией отсутствия фичи |
| 6 | Манипуляция низколиквидными | `Instrument.is_quote_eligible` нет в paper-версии — все perpetual USDT считаются eligible; фильтр по `MIN_24H` убран (был для spot). Для paper — все пары равнозначны, ликвидация защищает от манипуляции | — | — (не применимо к perpetual) |
| 7 | Двойное начисление / повтор крона | `trading_accounts` + `account_ledger INITIAL_BALANCE` idempotent `get_or_create_trading_account` (`trading_account.py:18` `begin_nested` + `IntegrityError` → re-read) + `CompetitionParticipant UNIQUE` | `test_paper_money.py:60` `test_demo_grant_is_idempotent`, `test_paper_race_pg.py:151` `test_two_finalizers_create_one_snapshot` | ✅ |
| 8 | Торговля без верификации | `services/accounts.py:12` `ensure_can_trade` (`phone_verified_at is None` → `PermissionError`) перед `paper_adapter` | `test_shared_market` + ручной `/trade` без `request_contact` → `Phone verification required` | ✅ |
| 9 | Забаненный продолжает торговать | `ensure_can_trade` `is_banned` в том же слое | `is_banned` check + `admin_ban` | ✅ |
| 10 | **Ликвидация / маржин-колл (новое для плеча)** | `workers/tp_sl_engine.py:60` `margin = notional/leverage`, `unrealized <= -margin*0.9 → LIQUIDATION` (90% buffer), `services/paper_adapter.py:454` `ExecutionReason.LIQUIDATION`, `close_position:489` cap `return_amount >=0` (`net = -margin` если `return<0`), `CHECK balance_after>=0` (`paper_models.py:90`) | `tests/test_liquidation.py:10` `test_high_leverage_no_crash_on_adverse_close` (300x LONG → crash to 1 → capped), `test_liquidation_engine_triggers` (300x LONG → 99.7 → liquidated), `test_liquidation_does_not_trigger_prematurely` | ✅ |

Дополнительно:
- `CHECK balance_after >=0` (`paper_models.py:90`) + `qty>0` — `test_liquidation.py`
- Дедлок-безопасность: одна строка `trading_accounts` (`paper_adapter.py:216`) — `test_paper_race_pg`
- Price poller resilience: `workers/price_poller.py:26` `_max_leverage_for_symbol` + `DEMO_WATCHLIST` 25 пар (было 950) → цикл <1с, `MARKET_DATA_MAX_AGE_MS=3000`
- `starting_equity` clean sheet: `services/competition.py:74` `join_competition` сбрасывает `TradingAccount` к `initial_balance` при входе в новый кубок (`test_competition_isolation.py:10`)
- Несколько позиций на актив: разрешено, `tp_sl_engine` per-position savepoint, `profile.py` показывает все (`test_competition_isolation.py:30`)
- `format_side` enum→string (`views.py:58`) — фикс `PositionSide.LONG` отображения
- `fmt_price` (`views.py:36`) — 2 знака для `>=1000`, 4 для `>=1`, 6 для `>=0.1`, 8 для `<0.1` + `price_precision` из `Instrument` (`UB 0.14→$0.14000`)
