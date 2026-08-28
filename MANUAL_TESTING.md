# MANUAL TESTING — единый демо-сценарий (paper trading, один сервис bot/main.py)

Архитектура после отката: **один Railway-сервис** = процесс `python -m bot.main`.
`price_poller`, `tp_sl_engine`, `competition_lifecycle` — фоновые asyncio-таски
в том же процессе (`asyncio.create_task`), та же PostgreSQL advisory-блокировка
(`workers/lock.py:LOCK_KEY`). Легаси TradeWeek-контур удалён из ветки `main`
(снапшот сохранён в ветке `legacy/tradeweek-snapshot`).

## 1. Локальный прогон тестов

```bash
pip install -r requirements.txt
pytest -q
```

Покрыто автотестами: ASK/BID-правила, Decimal-деньги, идемпотентность open/close,
гонки на реальном PG (`test_paper_race_pg.py`, скипается без Docker/TEST_DATABASE_URL),
плечо (маржа = бюджет), отказ при insufficient margin, идемпотентный демо-грант,
отказ исполнения без/с протухшим shared snapshot.

## 2. Локальный запуск (optional, sqlite без блокировок)

```bash
export BOT_TOKEN=...            # тестовый бот из BotFather
python -m bot.main              # предупреждение про singleton lock — ожидаемо
```

## 3. Railway (прод-приёмка)

Deploy = текущая ветка `main`. Start-команда (nixpacks.toml):
`alembic upgrade head && python -m bot.main`.
Проверка деплоя по логам:

- `Single-process bot starting: polling + price poller + TP/SL + competition lifecycle`
- Отсутствие ошибок advisory lock (`Another bot process already holds...`)
- Запись котировок: `SELECT symbol, bid, ask, exchange_timestamp FROM market_snapshots ORDER BY updated_at DESC;` — timestamps свежие каждые ~2с.

## 4. Сквозной сценарий (обязательная приёмка, на задеплоенном боте)

| # | Шаг | Ожидание |
|---|-----|----------|
| 1 | `/start` у нового юзера | Кнопка «Поделиться номером» (request_contact) |
| 2 | Поделиться контактом | «Номер подтверждён», демо-баланс $10 000, reply-меню с «Личный кабинет» |
| 3 | `/profile` | Юзернейм, баланс $10 000, 0 успешных / 0 неуспешных, ROE +0.00%, место в рейтинге |
| 4 | `/trade` → «1️⃣ Выбрать монету» → `SOL` | Ссылка `https://bingx.com/en/perpetual/SOL-USDT`, ведёт на график пары |
| 5 | `/trade` → «2️⃣ Быстрое открытие» → `SOL` → бюджет `100` → плечо `2x` → `LONG` → пропустить TP/SL → подтвердить | «Позиция открыта», вход по ASK |
| 6 | `/transactions` | Открытая сделка видна, PnL обновляется от живых тиков BingX (переоткрыть экран через 10–30с) |
| 7 | Закрытие вручную (или TP/SL) | LONG закрывается по BID, PnL зафиксирован |
| 8 | `/profile` после закрытия | Счётчик сделок увеличен, ROE пересчитан |
| 9 | Двойной клик по «✅ Подтвердить сделку» | Вторая позиция НЕ создаётся (in-flight защита + idempotency key) |
| 10 | Повторить 5–7 для SHORT | SHORT открывается по BID, закрывается по ASK |

Каждый пункт фиксируется скриншотом/логом реального диалога в Telegram
с задеплоенным ботом (см. ACCEPTANCE_EVIDENCE.md).
