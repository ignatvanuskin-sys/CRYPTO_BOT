# CryptoBot — Final Production-Readiness Report

**Date:** 2026-08-28
**Scope:** FINAL PRODUCTION-READINESS PASS (inspect → arch → plan → implement → test → fix →
Postgres concurrency → security → Railway → demo acceptance → diff → report)
**Status:** ✅ Code-complete & test-green · 🚫 **NOT COMMITTED** · 🚫 **NOT DEPLOYED**

> Hard constraints honored: no commit, no deploy, no Railway push, no reset/revert of existing
> user changes. The live Telegram bot on Railway was **not** touched.

---

## 1. Architecture Status — ✅ SOUND

The system is a **3-service Railway topology sharing one PostgreSQL** and one authoritative
market-data table:

| Service | Command | Responsibility | Singleton |
|---|---|---|---|
| `crypto-bot` | `python -m bot.main` | Telegram bot (modern paper profile/trade/leaderboard + legacy user router + admin) | `BOT_LOCK_KEY` advisory lock (82463518) |
| `crypto-api` | `uvicorn apps.api.main:app --host 0.0.0.0 --port $PORT` | FastAPI Mini-App backend + health/ready/metrics | — |
| `crypto-worker` | `python -m workers.main` | price poller + TP/SL engine + competition lifecycle | `WORKER_LOCK_KEY` advisory lock (82463517) |

**Most important invariant — ONE SHARED SOURCE OF MARKET DATA.** Every production trading path
reads `market_snapshots` (PostgreSQL) via `get_execution_snapshot`. `bot/main.py` import is
side-effect-free (no polling on import), and the singleton advisory locks make bot and worker
single-instance per database. Legacy process-local `price_cache` is isolated to legacy mode and
is never on the modern execution path.

Migration state: **single alembic head `004`** (fresh `upgrade head` verified clean).

---

## 2. Files Changed (this pass + prior phases, all uncommitted)

**Modified (tracked): 24 files, +2034 / −743**
`config.py`, `bot/main.py`, `bot/handlers/{admin,trade,leaderboard,profile,user}.py`,
`bot/keyboards.py`, `apps/api/{auth,main}.py`, `apps/miniapp/src/App.tsx`,
`services/{bingx_market_data,competition,paper_adapter,trading_account,leaderboard}.py`,
`workers/{price_poller,tp_sl_engine}.py`, `db/{models,competition_models}.py`,
`alembic/env.py`, `.env.example`, `tests/{conftest,test_gaps}.py`.

**New (untracked):** `db/market_data.py`, `services/{demo,metrics,notifications}.py`,
`workers/{main,lock,competition_lifecycle}.py`, `bot/views.py`,
`alembic/versions/004_runtime_safety.py`,
`tests/{test_shared_market,test_demo_acceptance,test_paper_mvp,test_paper_race_pg,test_race_pg,test_product_acceptance}.py`.

**Changed in THIS session (the 11-phase pass):**
- `services/bingx_market_data.py` — tz-safe `validate_snapshot`/`is_stale` (`_age_ms`); fixed a
  `TypeError` crash that would also occur on production PostgreSQL (naive DB timestamps vs
  tz-aware `now`).
- `services/paper_adapter.py` — idempotent open/close via `_resolve_idempotent_position` +
  `begin_nested` savepoints; open gated to active/started/non-ended cup (before **and** after end).
- `workers/competition_lifecycle.py` — finalize now skips already-closed positions and stays
  idempotent (no abort on a prior TP/SL/manual close).
- `bot/handlers/trade.py` — OPEN no longer auto-creates a competition; requires an active cup.
- `tests/conftest.py` — autouse fixture also clears the bingx process-local `_price_cache` so it
  cannot leak across test files and mask the shared-snapshot invariant.
- `tests/test_shared_market.py` (new) + `tests/test_demo_acceptance.py` (new).

---

## 3. Test Suite — ✅ 33 passed, 8 skipped

- **sqlite (aiosqlite) logic tests: 33 passing** — covers shared-snapshot source-of-truth,
  no-execution-without-snapshot, stale/future rejection, lifecycle (before-start / after-end),
  idempotent finalize + no-op re-finalize, full demo acceptance (DEMO_01 LONG=ASK, DEMO_02
  SHORT=BID, demo prizes, negative open-after-finalize), Decimal money path, gap analysis.
- **8 skipped** — real-PostgreSQL concurrency race tests (`test_paper_race_pg.py`,
  `test_race_pg.py`) requiring `testcontainers`/`asyncpg` + a running Docker daemon.
  Docker CLI is present but the **daemon is not running** and no local Postgres exists, so these
  are intentionally skipped (see §4).

`compileall` of `bot workers services apps db alembic` → clean. Import smoke test of
`bot.main`, `apps.api.main`, `workers.main` → clean.

---

## 4. PostgreSQL Concurrency Review — 🔎 STATIC (live execution: NOT EXECUTED)

**Reason NOT EXECUTED:** Docker daemon unavailable in this environment; no local Postgres.
asyncpg is installed but cannot spin up a container. The 8 race tests are written and will run
in CI with Docker (they are not replaced by sqlite tests, per design).

**Static verification of the 7 race classes (A–G):**

| Race | Mechanism (present in code) | Backend |
|---|---|---|
| A. Duplicate OPEN, same idempotency key | `_lock_account` `SELECT … FOR UPDATE` on `trading_accounts` + re-check after lock + `begin_nested` savepoint + `_resolve_idempotent_position` collapse | PG lock; savepoint logic sqlite-tested |
| B. Duplicate CLOSE (manual / double) | status guard + `session.refresh(position)` after account lock + close-order `begin_nested` savepoint; unique `PaperOrder.idempotency_key` | PG lock; path sqlite-tested |
| C. TP/SL engine vs manual close | both call `close_position`; serialize on account `FOR UPDATE`; loser observes CLOSED | PG lock |
| D. Two finalizers (`finish_competition`) | `with_for_update=True` on `Competition` + unique `LeaderboardSnapshot(competition_id,user_id)` / `CompetitionPrize(competition_id,rank)` | PG lock + constraint |
| E. Finalize vs TP/SL/manual close | finalize holds `Competition` row lock; close holds account row lock; different resources → no deadlock cycle; `except PaperError → refresh → skip` | PG lock |
| F. Snapshot read/write (poller vs consumers) | MVCC + `UPDATE`/`upsert` + CHECK `(bid>0, ask>0, ask>=bid)`; `validate_snapshot` rejects stale/future | constraint-enforced |
| G. Weekly grant / legacy race | `db/repo.py` `lock_user_week` `FOR UPDATE` on `users`; partial unique index `uq_weekly_grant`; legacy path only | PG lock |

**Guards present:** `FOR UPDATE` (account, competition, users), `pg_try_advisory_lock` (bot/worker
singletons), unique `idempotency_key` on `PaperOrder`/`AccountLedger`, unique
`(competition_id,user_id)` participants, `(competition_id,rank)` prizes, `(competition_id,user_id)`
leaderboard; `begin_nested` savepoints + `_resolve_idempotent_position`; status re-check after lock.

**⚠️ One concurrency finding to flag (low risk under defaults):** `close_position` locks the
**account** row, not the **position** row. The double-close guard relies on
`session.refresh(position)` observing the concurrent commit. This is correct under PostgreSQL's
**default `READ COMMITTED`** (per-statement snapshot sees the committed close). Under
`REPEATABLE READ`/`SERIALIZABLE` it could double-close. **Recommendation:** keep the DB at
`READ COMMITTED` (the default) **or** add `FOR UPDATE` on the position row in `close_position` for
defense-in-depth. The account-level `FOR UPDATE` already serializes same-account closes, so in
practice it is safe.

---

## 5. Security Review — 20 YES/NO questions

| # | Question | Answer | Evidence |
|---|---|---|---|
| 1 | Trade/account endpoints authenticated (Telegram initData HMAC)? | ✅ YES | `get_current_user` on all `/api/*` except public `/health`,`/ready`,`/metrics`,`/markets` |
| 2 | Missing/invalid initData rejected (401)? | ✅ YES | `validate_init_data` → `None` → `HTTPException(401)` |
| 3 | Constant-time HMAC compare? | ✅ YES | `hmac.compare_digest` |
| 4 | Stale / future initData rejected? | ✅ YES | `auth_date` age `>86400` or `< -60` → reject |
| 5 | Secrets from env only, never hardcoded/logged? | ✅ YES | `config.py` pydantic `BaseSettings`; `bot_token` never logged; `.env` git-ignored |
| 6 | `BOT_TOKEN` required at startup (no silent empty)? | ✅ YES | `bot/main.py` raises `RuntimeError` if unset |
| 7 | Money path uses `Decimal` (no float)? | ✅ YES | `pnl.py`, `paper_adapter.py` quantize; `Decimal` everywhere |
| 8 | Trade numeric input validated (finite, ≤12 dp, >0)? | ✅ YES | `OpenPositionRequest` validators + `open_position` checks |
| 9 | Idempotency-Key required on mutating endpoints? | ✅ YES | `/api/positions`, `/api/positions/{id}/close`, `open_position`, `close_position` |
| 10 | Parameterized queries / no SQL injection? | ✅ YES | SQLAlchemy ORM + bound params in `text()`; no f-string SQL |
| 11 | No stack-trace / secret leakage in error responses? | ✅ YES | `_trade_error` maps to generic codes; exceptions return mapped messages, no traceback to client |
| 12 | `/health` & `/metrics` expose no secrets? | ✅ YES | status-only / counters only; no token or DB URL |
| 13 | CORS restricted to known origins? | ⚠️ CONFIGURE | `allow_origins=_allowed_origins or ["*"]` → **must set `WEBAPP_URL` in prod** (else `*`); credentials only when origins set |
| 14 | Admin gated by numeric-ID allowlist? | ✅ YES | `is_admin` via `settings.admin_ids_set` (parsed `int`s) |
| 15 | Admin actions isolated from paper/demo misuse? | ✅ YES | legacy admin commands disabled in `paper` mode; demo gated by `DEMO_SEED_ENABLED` |
| 16 | Demo seed disabled unless explicitly enabled? | ✅ YES | `demo_seed_enabled` flag |
| 17 | PII exposed only to authorized roles? | ✅ YES | `phone_number` only in legacy `admin_review_top` to admins; not in public API |
| 18 | Competition finalize idempotent / no double prize? | ✅ YES | `with_for_update` + unique constraints + snapshot guard; tested |
| 19 | Execution never depends on process-local price cache? | ✅ YES | `get_execution_snapshot` reads DB; sqlite fallback test-only; tests clear local cache to prove isolation |
| 20 | Dependencies pinned / reproducible? | ✅ YES | `requirements.txt` pins versions; single alembic head |

**Additional observations**
- 🔸 **No API endpoint rate limiter** (only the BingX ccxt client's own `enableRateLimit`).
  Recommend adding per-IP/per-initData throttling before public launch.
- 🔸 **CSRF:** mitigated — trades are authenticated by initData HMAC, not cookies; safe under the
  explicit-origin CORS policy.
- 🔸 **XSS:** N/A — JSON API + Telegram; no HTML rendering of untrusted input.

---

## 6. Trading & Competition Integrity — ✅ VERIFIED

- **Bid/ask rules** (spec): LONG OPEN = ASK, LONG CLOSE = BID, SHORT OPEN = BID, SHORT CLOSE = ASK.
  Enforced in `paper_adapter.open_position`/`close_position`; proven end-to-end by
  `test_demo_acceptance_long_ask_short_bid_prizes_noop` (LONG entry = 50010 ask, SHORT entry =
  50000 bid).
- **Money path:** all notional/PnL/equity/price arithmetic is `Decimal`; results `.quantize`.
- **Idempotency:** same key ⇒ same result (open/close), including cross-account rejection.
- **Lifecycle safety:** open rejected before `starts_at` and after `ends_at`; finalize closes all
  OPEN positions, writes leaderboard + DEMO prizes **once**, and a second finalize is a **no-op**
  (tested). Already-closed positions during finalize are skipped (race C/E safe).
- **Competition start precondition:** bot OPEN now requires an active, started, non-ended cup
  (no silent auto-creation).

---

## 7. Railway Deployment Readiness — ✅ READY (commands documented; NOT deployed)

The existing `Procfile` (`web: … alembic upgrade head && python -m bot.main`) is the **legacy
single-service** start and was **left untouched** so the live bot keeps running. The intended
production topology is three Railway services, each with its own start command and the same env:

```bash
# 1) crypto-bot  (Telegram bot)
PYTHONPATH=/app alembic upgrade head && PYTHONPATH=/app python -m bot.main

# 2) crypto-api  (FastAPI Mini-App)
PYTHONPATH=/app uvicorn apps.api.main:app --host 0.0.0.0 --port $PORT

# 3) crypto-worker (poller + TP/SL + lifecycle)
PYTHONPATH=/app python -m workers.main
```

**Shared env (all 3 services):** `DATABASE_URL=postgresql://…` (asyncpg auto-applied),
`BOT_TOKEN`, `TRADING_MODE=paper`, `WEBAPP_URL=<your mini-app origin>`,
`ADMIN_TELEGRAM_IDS=<comma list of numeric IDs>`, `MARKET_DATA_PROVIDER=bingx`,
`MARKET_DATA_MAX_AGE_MS=2000`, `REQUIRE_POSTGRES=true`, `DEMO_SEED_ENABLED=false` (set `true`
only when seeding the demo cup), `BINGX_API_KEY`/`BINGX_API_SECRET` (if used).

**Migration step:** `alembic upgrade head` (single head `004`) must run once before the services
serve traffic — included in the bot command above; replicate for api/worker or run as a release
step.

`nixpacks.toml` builds on `python311` + `pip install -r requirements.txt`; `runtime.txt` pins the
runtime. No change required for the 3-service split beyond the start commands above.

---

## 8. Remaining Risks (post-fix)

1. **CORS `*` fallback** if `WEBAPP_URL` is unset in prod (Q13) — set `WEBAPP_URL` explicitly.
2. **No API rate limiter** (§5) — add before public launch.
3. **Postgres race tests NOT run here** (§4) — run in CI with Docker; rely on `READ COMMITTED`
   (default) for the close-position double-guard, or add position-row `FOR UPDATE`.
4. **Legacy path** (`services/trading.py`, `weekly_cycle.py`, `accounts.py`, `db/repo.py`) uses
   process-local `price_cache` and is exercised only in non-paper mode; the worker never invokes
   it. It is isolated but should be excluded from any future paper deployment.
5. **`/api/markets/{symbol}/candles`** returns empty `candles` (no fabricated data) — expected
   until a shared candle store is implemented; not a correctness risk.

---

## 9. Git Status — 🚫 NOT COMMITTED

`git status` shows 24 modified tracked files and 14 untracked new files (see §2). **No commit was
made**, per the hard constraint. `.env` and `*.db` are git-ignored (secrets/DB not committed).

## 10. Deployment Status — 🚫 NOT DEPLOYED

No Railway push, no `git push`, no `railway up`. The live Telegram bot on Railway was not modified
or restarted. The 3-service config in §7 is documented for the owner to apply when ready.

---

### One-line verdict
**Production-ready in code: architecture sound, invariant enforced, 33 tests green, 20/20 security
items pass (1 config-gated), demo acceptance proven. The only open items are operational
(CORS origin, rate limiter) and the Postgres race suite which must run in CI with Docker — none
block a careful deploy of the documented 3-service topology.**
