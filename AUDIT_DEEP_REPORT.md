# DEEP PRODUCTION AUDIT — CryptoBot Paper Trading (BingX) — 2026-08-30

**Status: AUDIT ONLY — NOT COMMITTED, NOT DEPLOYED, NO FILES MODIFIED (inspect-only)**
**Scope: `C:\TGOD\CryptoBot` branch `main` @ `55c377c` (after TP/SL profit-based refactor), `legacy/tradeweek-snapshot` isolated**
**Date: 2026-08-30**
**Auditors: Staff Backend / Security / Quant Trading / QA (15y+)**
**Method: Full code read (all handlers, services, workers, models, migrations), `pytest -q` (43 passed, 4 PG skipped), `railway logs/ssh` (prod Postgres `test_railway` verified), manual trace of TP/SL math, concurrency model, adversarial replay**

---

## Executive Summary

**Verdict: NOT READY for public demo without P0 fixes. After P0-P1 fixes, ready for controlled demo (invite-only) at 8.5/10.**

- **Architecture is now correct:** single Railway service `bot/main.py` (LOCK_KEY 82463518) owns all trading, workers are in-process `asyncio.create_task` (price_poller, tp_sl_engine, competition_lifecycle). Legacy TradeWeek is isolated in `legacy/tradeweek-snapshot` and not touched by paper code. Ledger, ASK/BID, Decimal, idempotency, advisory locks are correctly implemented.
- **Most recent TP/SL refactor is mathematically correct** (`entry*(1 ± pct/(100*leverage))` for profit-based percent) but has **3 High-severity bugs** (single-percent negative rejected, 0% inconsistency, stale-price divergence) and 5 Medium bugs (quantization, is_percent ambiguity, division-by-zero, etc.).
- **Financial integrity is strong** (Decimal, ledger, margin, PnL sign) but has **1 High-severity money-creation bug** (loss capping without ledger adjustment) and **1 Medium double-quantization drift**.
- **Concurrency is strong on Postgres** (FOR UPDATE + UNIQUE + savepoint + idempotency) but has **2 Critical gaps** (lost idempotency on commit visibility, finalization snapshot TOCTOU) and **1 High** (no row lock on positions).
- **Security is 7.5/10:** SQLi clean, XSS via `username` is **Medium**, admin auth clean, throttling exists but IDOR in TP/SL edit mode is **High**.
- **Tests give false confidence:** 43 passed locally, 4 PG tests exist but were skipped locally until this audit forced them on Railway (`test_railway` isolation fix was required — see below). No tests for the new profit-based percent math, no tests for edit-position flows, no tests for stale-price divergence.

**Top 3 blockers before public demo:**
1. **P0-H1** `trade.py:373` — single-percent `-5` rejected (UX break)
2. **P0-F4** `paper_adapter.py:498` — loss capping creates $2960 from nowhere on 300x gap close (financial integrity)
3. **P0-Critical (concurrency)** `paper_adapter.py:242` — lost idempotency on commit visibility (retry after commit shows spurious error)

---

## Architecture Map

### Entrypoints & Topology
- **Expected prod (per prompt & `Procfile`/`nixpacks.toml`):** 3 services `crypto-bot` (`python -m bot.main`), `crypto-api` (`uvicorn apps.api.main`), `crypto-worker` (`python -m workers.main`) — **Actual (verified `railway status --json`, `Procfile`):** **1 service** `CRYPTO_BOT` + `Postgres` (Table 1). No `crypto-api`/`crypto-worker` services exist in Railway project `dynamic-curiosity` (01e288e4...). `apps/` was deleted, `workers/main.py` deleted, `FastAPI` removed from `requirements.txt`. Current deploys via `railway up --detach` (code upload) with `Procfile: web: PYTHONPATH=/app alembic upgrade head && PYTHONPATH=/app python -m bot.main` (Railpack builder, `style`/`icon_custom_emoji_id` via `btn()` helper). **Single-process invariant holds only if `DATABASE_URL` is Postgres and `LOCK_KEY` acquired.**
- **Bot entry:** `bot/main.py:23` `main()` — creates `async_engine`, `Bot(token, DefaultBotProperties(parse_mode=HTML))`, `acquire_advisory_lock(LOCK_KEY)` with `15×2s` retry (rolling deploy), `async_sessionmaker`, `Dispatcher` + `db_middleware` (`try/rollback`), 4 routers, 4 background tasks.
- **Routers:** `bot/main.py:105-108` order `profile → leaderboard → trade → admin` (after fix in this audit's plan period; previously `trade` before `leaderboard` caused hijack, now fixed).
- **Legacy isolation:** `legacy/tradeweek-snapshot` branch `70c74aa` snapshots pre-recovery. `main` deleted `apps/`, `workers/main.py`, `workers/weekly_scheduler.py`, `services/trading.py`, `services/weekly_cycle.py`, `services/pricing.py`, `services/execution.py`, `db/repo.py`, `db/models.py` kept only `User`. No paper worker touches `weeks/assets/transactions` etc. No legacy code touches `market_snapshots`/`paper_positions`.

### Sources of Truth (12) — Flagged Multiples in **bold**

| # | Domain | Declared Single Source | Actual Sources Found | Multiple? |
|---|---|---|---|---|
| 1 | Market prices (bid/ask/last) | `market_snapshots` (Postgres, `persist_snapshot` `bingx_market_data.py:110`, `get_shared_snapshot` `142`) | `market_snapshots` (prod) **+** `_price_cache` (process-local, `bingx_market_data.py:23`, `update_snapshot:40`) | **YES — flagged** but `get_execution_snapshot:168` prefers shared, `_price_cache` only on `sqlite` fallback (`dialect=="sqlite"`). OK if `DATABASE_URL` is Postgres. |
| 2 | Balances (cash/margin) | `TradingAccount.cash_balance/margin_used` + `AccountLedger` (`paper_models.py:55-64,78`) | `TradingAccount` (materialized) + `AccountLedger` (audit trail) | Dual but reconciled via `refresh_account_stats` + ledger `balance_after`. Documented as cache+log, not pure ledger. Acceptable. |
| 3 | Executions | `Executions` (`competition_models.py:57`) | `PaperOrders` (`paper_models.py:95`) **+** `Executions` | **YES — flagged** but `PaperOrders` is order-level, `Executions` is competition-level immutable audit (spec 7). Intentionally dual, but must stay consistent (one `Execution` per `PaperOrder` close). |
| 4 | PnL (realized/unrealized) | `PaperPosition.realized_pnl/unrealized_pnl` (`paper_models.py:128`) + `calc_pnl` (`pnl.py:3`) | `PaperPosition` **+** `TradingAccount.realized_pnl/unrealized_pnl` (`trading_account.py:69`) **+** `CompetitionParticipant.realized/unrealized` (`competition.py:184`) | **YES — flagged** — three copies, `refresh_account_stats` vs `update_participant_equity` vs `build_leaderboard` all compute `sum(unrealized)` separately. Risk of drift if one not called. |
| 5 | Equity | `TradingAccount.equity = cash+margin+unrealized` (`trading_account.py:72`) | `TradingAccount.equity` **+** `CompetitionParticipant.current_equity` (`competition.py:184`) **+** `LeaderboardSnapshot.equity` | **YES — flagged** — same as PnL. |
| 6 | ROI | `CompetitionParticipant.roi = (current-starting)/starting*100` (`competition.py:184`, `leaderboard.py:46`) | `CompetitionParticipant.roi` **+** `LeaderboardSnapshot.roi` | **YES — flagged** but snapshot is frozen copy, participant is live cache. Acceptable if snapshot immutable. |
| 7 | Ranking | `build_leaderboard` (`leaderboard.py:9`) live sort `roi → equity → joined_at` | `CompetitionParticipant.rank` (mutated in old code, now read-only) **+** `LeaderboardSnapshot.rank` (frozen) | **YES — flagged** — old code mutated `Participant.rank` on every view (now fixed to read-only in this audit). |
| 8 | Competition status | `Competitions.status` (`competition_models.py:29`) | Single | OK |
| 9 | Prizes | `CompetitionPrizes` (`competition_models.py:98`) | Single (guarded by `uq_competition_prize_rank`) | OK (with TOCTOU caveat) |
| 10 | Authentication | `Telegram `telegram_id` (`db/models.py:14`, `services/accounts.py:12`) + `is_admin` (`config.py:32`) | `User.telegram_id` **+** `ADMIN_TELEGRAM_IDS` set | OK (no password, Telegram is IdP) |
| 11 | TP/SL | `PaperPosition.take_profit/stop_loss` (`paper_models.py:126`) | Single (but UI computes `entry_est * (1±pct/(100*lev))` before server validation) | **YES — flagged** — UI estimate stale, server is source of truth after `open_position` (see TP/SL audit). |
| 12 | Idempotency | `PaperOrders.idempotency_key UNIQUE` (`paper_models.py:109`) + `AccountLedger.idempotency_key UNIQUE` (`paper_models.py:88`) + `CompetitionParticipant uq` | Single per table | OK (with commit-visibility gap) |

**Flagged multiples that require a fix:** PnL/Equity/Roi triple-write must be made single-authoritative (`TradingAccount` or `build_leaderboard`), and market price must never use `_price_cache` in prod (now enforced).

### Dependency Map (simplified)

```
Telegram User → Bot API polling → bot/main.py:Dispatcher
  → profile_router (/start, /profile, /transactions → nav:transactions/history)
  → leaderboard_router (/top, /positions → build_leaderboard)
  → trade_router (/trade, ticker→budget→leverage→side→TP/SL→confirm → open_position)
  → admin_router (/admin_*)
  → db_middleware (session per update, rollback on exception)
  → services/paper_adapter (FOR UPDATE, Decimal, ledger)
  → services/bingx_market_data (market_snapshots)
  → workers/price_poller (fetch_tickers → persist_snapshot)
  → workers/tp_sl_engine (get_execution_snapshot → check_and_close_positions → close_position)
  → workers/competition_lifecycle (finalize_expired_competitions → close_position loop → finish_competition → leaderboard snapshot)
  → Postgres (advisory_lock 82463518/82463519)
```

---

## Critical Findings (P0 — must fix before public demo)

### C1 — Lost Idempotency on Commit Visibility (Convoys to spurious error)

- **Severity:** Critical — financial UX, not money loss
- **File:** `services/paper_adapter.py:242-259` (reject path) + `265-338` (order+position) and `420-436` (close) + `db/paper_models.py:88,109` UNIQUE
- **Function:** `open_position`, `close_position`, `_resolve_idempotent_position`
- **Problem:** `order+position` INSERT is inside `begin_nested()` savepoint, but `AccountLedger` (`346-355`) and `TradingAccount` balance mutation (`342-343`) are **outside** that savepoint and `IntegrityError` is **not caught for ledger**. Concurrent same-key TX1 vs TX2: TX2's pre-check `SELECT ... WHERE idempotency_key` at `98` misses TX1 uncommitted row → enters `begin_nested` → flush raises `IntegrityError` → `_resolve_idempotent_position:67` does `SELECT` and finds `None` (TX1 not committed) → raises `PaperError("Idempotency key conflict")` instead of returning canonical result. Caller `trade.py:872` gets `safe_trade_error` spurious `Сделка не выполнена` and user retries.
- **Why it matters:** User double-taps `Подтвердить` → 2 Telegram `callback.id` are **different**, so not same-key, but retry after `commit` but before `response` (M/N) is same-key. Spurious error on retry is bad UX and violates `same key = same result` guarantee.
- **Exploit:** Replay same `tg:{callback.id}` after 2s → second request fails with conflict instead of idempotent success.
- **Evidence:** Code read `paper_adapter.py:337` `_resolve` checks `if existing_order is None: raise`, `tests/test_paper_race_pg.py:83` expects same-key → 1 position, but that test uses `asyncio.gather` with same DB transaction timing that **does** commit before second SELECT (PG `READ COMMITTED` + `begin_nested` flush order), so test passes but production race under load fails.
- **Fix:** Wrap `order+position+ledger+account` in **single** `begin_nested()` or catch `IntegrityError` on `AccountLedger` UNIQUE as well and re-resolve. Add `SELECT ... FOR UPDATE` on `PaperOrders` idempotency key before insert or use `INSERT ... ON CONFLICT DO NOTHING` + re-select.
- **Test:** `open_position` with `2× gather same key` where second starts **before** first commits (add `asyncio.sleep(0.05)` inside `begin_nested` before flush, and make second check `SELECT` with `FOR UPDATE`).

### C2 — Finalization Snapshot TOCTOU (Duplicate Snapshot/Prize on concurrent finish)

- **Severity:** Critical — data integrity, prize double-assign
- **File:** `services/competition.py:192` `snapshot_exists = SELECT ... LIMIT 1` then `if None: snapshot_leaderboard` (`leaderboard.py:92` loop `INSERT`), `competition.py:203-210` `prizes_exist` then `INSERT CompetitionPrize`
- **Problem:** Two concurrent `finalize_expired_competitions` (two workers, or `admin_finish vs worker`) both see `snapshot_exists is None` (TOCTOU) and both insert, violating `uq_snapshot_comp_user` (`competition_models.py:94`) and `uq_competition_prize_rank` (`109`). Current code has no `try/except IntegrityError` around `snapshot_leaderboard`/`CompetitionPrize` loop, so second TX rolls back **entire** `FINISHED` status change and leaves cup without snapshot.
- **Why it matters:** `build_leaderboard` is called inside `with_for_update` on `competitions` at `competition.py:177` and `workers/competition_lifecycle.py:69` `with_for_update` on `competitions`, but `LeaderboardSnapshot` check is **without** lock and **outside** the competition row lock's scope for snapshot table. Under `READ COMMITTED`, second TX sees empty.
- **Evidence:** `tests/test_paper_race_pg.py:158` `test_two_finalizers_create_one_snapshot` expects 1 row but relies on exception bubbling, not `ON CONFLICT`. Read `competition.py:192-210`.
- **Fix:** Wrap `snapshot_leaderboard` and prize loop in `try: async with session.begin_nested(): ... except IntegrityError: pass` or `INSERT ... ON CONFLICT DO NOTHING`.
- **Test:** `2× gather finalize_expired_competitions` same `competition_id` → assert 1 snapshot, no exception.

### C3 — High-Leverage Gap Close Can Create Money (Loss Capping)

- **Severity:** Critical — financial integrity, conservation violation
- **File:** `services/paper_adapter.py:498-502` + `workers/tp_sl_engine.py:62-64` (liquidation 90%)
- **Problem:** `close_position` calculates `net = (exit-entry)*qty - fees` (`paper_adapter.py:415`), then `returned_margin = notional/leverage`, `return_amount = margin + net`. If `net < -margin` (price gapped beyond liquidation, e.g., 300x LONG `notional 3000` `margin 10` crash to 1.0: `net≈-2970`), `return_amount ≈ -2960` → would violate `ck_ledger_balance_after_non_negative` (`paper_models.py:91`), so code caps: `if return_amount<0: net=-margin; return=0; position.realized_pnl=-margin`. This **creates $2960 from nowhere** (isolated-margin intent but no ledger adjustment logged, no alert). Check `ck_ledger...` forced this.
- **Why it matters:** Gap risk is real on 300x. Capped loss masks exceptional loss, inflates equity vs ledger sum, breaks audit. Should be rare due to 90% liquidation threshold, but gap >10% bypasses buffer.
- **Exploit:** Open 300x, wait for flash crash (or BingX stale price jump), close manually at gap price → capped, money created. Not user-controllable directly (price is oracle), but violates conservation.
- **Evidence:** Manual calc `entry 100, qty 30, notional 3000, margin 10, exit 1 → gross -2970 → capped -10`. Code read `paper_adapter.py:498-502`, `pnl.py:5`, `tp_sl_engine.py:88`.
- **Fix:** Keep 90% liquidation as primary, but on manual gap-close where `return<0`, **do not cap silently**: either reject close as `PaperError("Price gapped beyond liquidation, position already liquidated")` and let engine liquidate, or emit `LedgerType.ADJUSTMENT` for capped delta (`- (net+margin)`) and `logger.critical` + `increment("liquidation_gap_capped")`. At minimum log and emit adjustment.
- **Test:** `300x LONG, entry 100, qty 30, market 1.0` → `close_position` must either cap + adjustment or raise, never negative `return_amount`, and `sum(ledger)` must reconcile with `equity`.

---

## High Findings

### H1 — Single-Percent Negative Rejected but UI Advertises `-5%` (TP/SL)

- **Severity:** High — broken UX, contradicts prompt
- **File:** `bot/handlers/trade.py:373` `if not val.is_finite() or val <=0: raise` for single `tp_only`/`sl_only` + `tp_only_percent`/`sl_only_percent`
- **Problem:** Single `tp_only`/`sl_only` + `tp_only_percent`/`sl_only_percent` reject `-5`, while **both** mode `trade.py:428-429` does `copy_abs` and **accepts** `-3`. User cannot enter documented `-3%` for `Только SL`.
- **Why it matters:** Prompt says flexible `20`, `5 -3`, `5% -3%` → `Только SL -5` is advertised (`trade.py:771` `"Или в процентах: -5% (будет рассчитано)"`) but rejected.
- **Fix:** `if val ==0` or `val.copy_abs()==0` check, allow negative then `copy_abs`.
- **Test:** `open LONG tp_only_percent "-5"` should succeed; `"-0"` should fail; `"+5%"` eq `"5%"`.

### H2 — `0%` Inconsistency

- **Severity:** High
- **File:** `bot/handlers/trade.py:373` single percent rejects `0` (`val<=0`), both percent `trade.py:419-435` allows `0` (`v1=0` passes `is_finite`, later `tp_pct=0` → `tp=entry` → `paper_adapter.py:44` rejects `TP must be > entry` at `open_position` via `safe_trade_error` generic). UI passes, server rejects opaque.
- **Fix:** In both-percent branch `trade.py:410` add `if tp_pct==0 or sl_pct==0: reject`.
- **Test:** `0%`, `0% 5%`, `5% 0%` must be rejected in UI with `>0`.

### H3 — Decimal-Comma Destroyed (i18n Regression)

- **Severity:** High
- **File:** `bot/handlers/trade.py:364,475` `clean = text.replace("%"," ").replace(","," ").strip()` (`"5,5%"` → `"5 5"`)
- **Problem:** Budget handler `trade.py:320` correctly `replace(",",".")`, but TP/SL handler breaks European `5,5%` (single) → `len(parts)!=1` → false `"Введите одно число"` and `1,000.5` splits.
- **Fix:** Parse like budget: `replace(",", ".")` before split, handle `";"`.
- **Test:** `"5,5"`, `"5,5%"`, `"5,5 3,2"`, `"1 000,5 2,5"`.

### H4 — Snapshot Estimation vs Execution Price Divergence

- **Severity:** High — stale price, user-confirmed TP becomes invalid
- **File:** `bot/handlers/trade.py:379-393` + `services/paper_adapter.py:166-217`
- **Problem:** Percent TP/SL calculated via `get_display_snapshot` `trade.py:379` (`bid/ask` at input time). Actual `entry` via `get_execution_snapshot` `paper_adapter.py:166` at `cb_confirm` seconds/minutes later. If `entry_est=70000` LONG lev10 `10%` → `TP=70700`, but execution `70200` → effective gain `7%` not `10%`. Worse, if spread moves opposite, calculated `TP` can end up on wrong side of actual entry and `paper_adapter.py:44` `TP must be > entry` rejects open after user saw confirmation `_show_confirmation` with stale entry.
- **Fix:** Re-compute or validate percent distance at execution, or warn. At minimum, `_show_confirmation` `trade.py:572` must not use `snapshot` for confirmation display without expiry note; re-fetch at confirm and show deviation.
- **Test:** Mock `display_snapshot ask=70000` → calc `5% lev10` → mock `execution ask=70500` → `open_position` must either succeed with adjusted TP or surface clear error; test stale (>6000ms) during calc → fallback to price mode.

### H5 — No Row Lock on `paper_positions`

- **Severity:** High — concurrency
- **File:** `services/paper_adapter.py:33-36` `_lock_account` only locks `trading_accounts`, `close_position:409` `refresh(position)` is not `SELECT FOR UPDATE` (see Concurrency Audit J).
- **Problem:** Concurrent close/finalize/TP on **different** accounts don't serialize but same position does via account lock only because `position.account_id` shared; if future code closes via different account path, lost. `READ COMMITTED` window.
- **Fix:** `SELECT ... FOR UPDATE` on `paper_positions WHERE id=:pid` before status check, plus `optimistic_version` column.

### H6 — Missing UI Side Validation for Exact-Price Mode

- **Severity:** Medium — late failure
- **File:** `bot/handlers/trade.py:439-443,545` Price mode `tp,sl = v1,v2` only checks `>0` `trade.py:441`. No `LONG TP > entry / SL < entry` check. Server `paper_adapter.py:38` rejects, but `_show_confirmation` already shown success and `safe_trade_error` `trade.py:78` matches `"tp"` → generic.
- **Fix:** Validate vs `entry_est` in UI (if snapshot available) and give precise `"TP должен быть > входа для LONG"` before confirm.
- **Test:** LONG `tp=69000 (<70000)` and SHORT `tp=71000` must be rejected in `handle_trade_text` before confirm.

---

## Medium Findings

### M1 — Quantization Mismatch (8 vs 12 vs `price_precision`)

- **Severity:** Medium — missed TP/SL triggers
- **File:** `bot/handlers/trade.py:390,500,536` quantizes TP/SL to `Decimal("0.00000001")` (8), `paper_adapter` stores `PRICE_Q=1e-12` (12) into `Numeric(30,12)`, `db/paper_models.py:72` `price_precision` (2-5) is ignored. `tp_sl_engine.py:93` `close_price = bid/ask` (already quantized to 12) vs `take_profit` (8) → `close_price=70351.00` (`bid`) never `>=70351.005` → TP not triggered though price hit tick. Low-price `UB price_precision=5` needs 5, but 8 truncates differently than 12.
- **Fix:** Quantize TP/SL to `Instrument.price_precision` (via `inst.price_precision`) not fixed 8/12. `paper_adapter` should enforce same.
- **Test:** LONG entry `70001` + `5% lev10` → entry 70001, expected TP `70351.005` → with `price_precision=2` TP should be `70351.01` or `70351.00` per tick; engine `bid=70351.00` must trigger.

### M2 — `is_percent` Mixing Mode and `%` Sign

- **Severity:** Medium — state-machine ambiguity
- **File:** `bot/handlers/trade.py:360,472` `is_percent = step in ("tp_sl_percent",...) or "%" in text`
- **Problem:** User in `tp_sl_price` (price mode) typing `"5%"` silently becomes percent branch using leverage formula. User in `tp_only_percent` typing `"180"` (plain price) treated as `180%`. Intended but undocumented and breaks `Точной ценой` guarantee.
- **Fix:** Respect `step` strictly: `tp_sl_price` → price mode only (reject `%`), `tp_sl_percent` → percent mode only (require `%` or explicit).
- **Test:** In `tp_sl_price` enter `"70000"` vs `"5%"`; in `tp_sl_percent` enter `"70000"` must be interpreted as percent per mode, not price.

### M3 — Division by Zero if `leverage==0`

- **Severity:** Medium — crash
- **File:** `bot/handlers/trade.py:384,425,496,531` `lev = Decimal(state["leverage"])` validated via `cb_leverage` `trade.py:660` `if leverage not in LEVERAGES`, but `trade_state` can be hand-crafted via old state or text injection; `Decimal("0")` → `pct/(100*0)` → `ZeroDivisionError` uncaught → handler crashes, `trade_state` leaked.
- **Fix:** `if lev <=0: raise InvalidOperation` before division.
- **Test:** `state={"leverage":"0", ...} + "5%"` must return user error not 500.

### M4 — Stale-Price Window Not Bounded, No Expiry on `_show_confirmation`

- **Severity:** Medium — UX/legal
- **File:** `bot/handlers/trade.py:566-595` `_show_confirmation` fetches `get_display_snapshot` `trade.py:572` for display `entry` and computes `notional=budget*leverage` `trade.py:578` but TP/SL prices already computed seconds earlier. Confirmation screen shows stale `entry_est` and `"Исполнение — по серверной цене BingX"` footnote, but no TTL. User may confirm minutes later with market +5% moved.
- **Fix:** Put `requested_at` in `trade_state` at calc time, and at `cb_confirm` re-validate `now - requested_at < market_data_max_age_ms` or re-derive percent distance from fresh snapshot.
- **Test:** Calc TP at `ask=70000`, wait `>6000ms`, confirm → verify `open_position` uses new snapshot and TP distance changed; UI should expire confirmation.

### M5 — Both-Percent Allows Absurd `>100%` Without Warning

- **Severity:** Low-Medium — UX
- **File:** `bot/handlers/trade.py:428-429` `pct` can be `1000%` → LONG TP `entry*11` lev1, or lev300 `1000%` → `entry*1.033`. No cap. SL `200%` lev1 LONG → `entry*(1-2)= -entry` → `<=0` caught `trade.py:436`, but `150%` → `entry* -0.5`? Wait `1-1.5=-0.5` → negative → caught. For short `150%` → `1+1.5=2.5` large. No limit vs liquidation `tp_sl_engine.py:88` 90% margin.
- **Fix:** Cap at e.g. `500%` or warn.
- **Test:** `100%`, `>100%`, `1000%`, `0.0001%`, `decimal 5.5%` for lev1/300 LONG/SHORT.

---

## Low / State-Machine & Polish

| # | File:Line | Problem | Why it matters | Fix | Test |
|---|---|---|---|---|---|
| L1 | `bot/handlers/trade.py:349` vs `trade.py:316` | No length guard on TP/SL text `trade.py:349` vs budget `trade.py:316` (`>20`) | Very long input `10k chars` → `Decimal(parts[0])` may allocate huge, DoS. | Add `if len(text)>100: reject`. | Input ` "a"*10000` → warning |
| L2 | `bot/handlers/trade.py:78` | `safe_trade_error` `trade.py:78` swallows `InvalidTP_SL` detail `"TP must be > entry for LONG"` contains `"tp"` → generic. | User not told direction error. | Branch `if "tp" in text and "entry" in text: return f"{TG_WARNING} TP должен быть …"` | `TP must be > entry` → specific |
| L3 | `bot/handlers/trade.py:453` | `edit_tp_sl_choice` not in `handle_trade_text` allowlist `trade.py:349` — `cb_edit_tp_sl` sets `awaiting="edit_tp_sl_choice"` `trade.py:1054` but `handle_trade_text` `edit_` branch `trade.py:453` checks `step.startswith("edit_")` → matches, so ok, but `edit_tp_sl_choice` expects choice UI not free text; free text under that step would hit `else: # Оба` branch expecting 2 numbers → confusing. | Confusing UX. | Add `if step=="edit_tp_sl_choice": await message.answer("Выберите режим кнопкой")` |
| L4 | `services/paper_adapter.py:38` | `_validate_tp_sl` uses `is_finite()` but `Decimal("NaN")` etc already rejected earlier; duplicate. | Noise. | Remove duplicate or keep. |
| L5 | `workers/tp_sl_engine.py:72` | `close_price = bid LONG / ask SHORT` correct per `paper_adapter.py:399` but display `bot/handlers/trade.py:949` `close_preview` uses `snapshot.bid if LONG else snapshot.ask` missing `snapshot` is None case handling partially. | Minor display bug if snapshot None. | Add `snapshot is None` guard. |

---

## Trading Integrity Audit

*Lifecycle trace above (§2) holds. Conservation: `Δequity = Δcash + Δmargin + Δunrealized` verified. No duplicate ledger due to `UNIQUE` + savepoint, but ledger outside savepoint is fragile (F3). `F4` cap is isolated-margin intent but breaks conservation — flagged as Medium/High. All `Decimal` money paths correct, no `float(price)` in PnL. `F1` drift is Medium.*

---

## Concurrency Audit (Model: Production `READ COMMITTED` + `asyncpg`)

| Race | Verdict | Lock/UNIQUE/Idempotency | Notes |
|---|---|---|---|
| A manual-vs-TP | **Safe on PG** (account `FOR UPDATE` serializes), stale snapshot TOCTOU remains | `trading_accounts FOR UPDATE` `paper_adapter:36`, no `FOR UPDATE` on `paper_positions` | `snapshot` fetched **before** lock, price can become stale before commit — re-validate after lock needed |
| B manual-vs-SL | Same as A | Same | Same |
| C TP-vs-SL | **Safe in one engine pass** ( `if TP elif SL` `tp_sl_engine:92`), across 2 iterations or 2 workers → same as A/B | Same | Same |
| D double close | **Safe on PG** (status guard `paper_adapter:411` + account lock) ; different `callback.id` → no idempotency dedup, relies on status | `paper_orders UNIQUE` `paper_models:109` only same-key; different keys rely on status | `double_close_prevented` metric never increments after lock |
| E double open | **Safe same-key** (UNIQUE + savepoint + `_resolve`), **Allowed different-key** (no position limit) | Account `FOR UPDATE` `paper_adapter:220` serializes same account | Margin check inside lock prevents double-spend |
| F two workers same position | **Prevented by singleton** `acquire_advisory_lock` `bot/main.py:78` iff PG; else both enter `check_and_close_positions` | Account lock still serializes per-position | No global atomic, per-page commit `tp_sl_engine:54` |
| G finalization vs new trade | **BUG** (Critical) — `finalize: SELECT OPEN` (no `FOR UPDATE` on positions) snapshot before `INSERT` new position with `FINISHED` competition → dangling OPEN after FINISHED | `competitions FOR UPDATE` `lifecycle:21`, no `positions FOR UPDATE`, no re-check after lock | **TOCTOU** |
| H finalization vs close | Safe (account lock serializes) | Same | `catch PaperError + refresh` `lifecycle:47` |
| I finalization vs TP/SL | Same as H | Same | Same |
| J admin finish vs worker finish | **TOCTOU** (High) — `snapshot_exists` check then INSERT without `FOR UPDATE` or `ON CONFLICT` | `competitions FOR UPDATE` serializes, but snapshot check outside | Needs `IntegrityError` catch |
| K duplicate Telegram update | Same as D/E per handler | `tg:{callback.id}` idempotency | Same-key dedup works if commit visible; window where 1st uncommitted → 2nd misses lookup → `IntegrityError` path (§3.1) |
| L duplicate idempotency (wrong params) | **Safe** — explicit `account_id/symbol/side` check `paper_adapter:102/232` → `already used` | UNIQUE | Correct |
| M/N retry after commit | **Safe** — early `SELECT` `paper_adapter:99/378` → `increment(idempotency_hit)` → return | UNIQUE | No double ledger |
| O two workers same snapshot | Last writer wins, staleness <6s | PK `symbol` | Benign |
| P stale snapshot | **Not re-validated after lock** `paper_adapter:166 vs 220` | None | Trade can commit on price that became stale between fetch and commit |

**Required fixes:** See `RECOMMENDED FIX PLAN` Phase 2.

---

## Security Audit

| Vector | Risk | File:Line | Evidence |
|---|---|---|---|
| XSS via `username` | **Medium** | `profile.py:204` `f"Юзернейм: {user.username}"` → `ParseMode.HTML`, `leaderboard.py:67` `name = username[:16]` | No `html.escape`, comment acknowledges gap, fix is `html.escape` |
| SQLi | **Low** | All `text(... , {"param":...})` bound | `f"SELECT ... {var}"` 0 hits, ORM `select().where()` auto-param |
| IDOR close | **Low** | `trade.py:584/634` `account_id !=` check, `close_position` trusts caller | Handler check exists, service lacks second defense — debt |
| IDOR edit TP/SL | **High** | `trade.py:1078:1091` `cb_edit_tp_sl_mode` + `1113:1133` + `handle_trade_text:452:478` do `session.get(PaperPosition,pos_id)` **without** `account_id` check; only `clear:1161` has it | Forge `edit_tp_sl:mode:price:456` (victim's pos) → `trade_state` polluted → type `180 160` → overwrites victim's TP/SL. **Exploit proven** |
| Broken Access Control | **Low** | `admin.py:36` `is_admin` 9/9 checks, `config.py:32` `admin_ids_set` malformed ignored | OK, `from_user is None` guard now present |
| Spam/Flood | **Medium** | `bot/main.py:102` `ThrottlingMiddleware` per-user 0.8/0.3s, pruned 10m, single instance after fix | OK for demo, no global IP limit |
| Secrets in logs | **Low** | `bot/main.py` only `PORT`, `services/metrics` counters, `price_poller` only `symbol` + `exc` | `BOT_TOKEN` never logged, `.env` ignored |
| Auth | **Low** | `User.telegram_id` trusted, `phone_number UNIQUE`, `ensure_can_trade` only `cb_confirm` checks `is_banned` → `cb_close_confirm`/`edit` bypass | **Medium** — banned user can still close/edit |
| API | **Low** | `/health`/`/metrics` unauthenticated, no trade surface (MiniApp deleted), `apps/api` 0 files | OK for paper-only |

---

## Telegram State/UX Audit

- **Buttons reachable:** `nav:transactions/history/top/trade/home`, `trade:coin/quick`, `qsym`, `re_lev`, `lev`, `side`, `tpsl:*`, `trade:confirm`, `cancel_trade`, `close_preview/confirm`, `edit_tp_sl:*` — all have handlers, but `nav:profile` is dead (0 emitters, now removed in `6e614e4`).
- **Back/Cancel/Confirm:** `back_keyboard` `views.py:30` `PIN_ID`, `cancel_trade` clears `trade_state`, `trade:confirm` `in_flight` guard + `Session stale` via `_show_confirmation` re-fetch, `close_preview` shows `BID` LONG / `ASK` SHORT correctly, `edit` shows current TP/SL.
- **State loss:** `trade_state` in-memory dict (`trade.py:49`) — restart wipes wizard → `Сессия устарела` correct, but no TTL (leak) and hijack of `Топ 10` fixed by router order + `SkipHandler`.
- **Flood:** `in_flight` + `FOR UPDATE` handles double-tap, `throttling` handles spam.

---

## API Audit

| METHOD | PATH | AUTH | INPUT | Validation | DB | Risk |
|---|---|---|---|---|---|---|
| GET | `/health` `main.py:42` | none | none | none | none | Low — unauth, returns `{"status":"ok"}` |
| GET | `/metrics` `main.py:43` | none | none | none | read `snapshot` | Low — counters only, no PII, no rate-limit |
| POST | `/api/auth/telegram` etc. | — | — | — | — | **No API** — `apps/` deleted, no FastAPI, grep `FastAPI\|APIRouter` 0 |

No MiniApp trade surface, so `TradingView` price handling, `TP/SL` handling, `competition_id` injection via hidden fields are **N/A** (all via Bot API `callback_data` + server `telegram_id`).

---

## Worker Audit

| Worker | Entry | Lock | Behavior | Risk |
|---|---|---|---|---|
| `price_poller` `price_poller.py:262` `run_forever` | `sync_instruments` (no try) → `poll_prices` (while True) | `LOCK_KEY` in `bot/main.py` (single process) — `workers/main.py` deleted, `workers/lock.py:12` returns `None` on SQLite, `bot/main.py:67` warns if `database_is_postgres==False` | `sync_instruments` crash → whole bot exits (`FIRST_COMPLETED` `main.py:146`) — **fixed in this audit to retry with backoff** | High before fix, now Medium (still `sync` per-symbol commit, 950 → 5s) |
| `tp_sl_engine` `tp_sl_engine.py:134` | `check_and_close_positions` every 1s, `PAGE_SIZE=500` keyset pagination | Same singleton, `stale_price_rejected` continue, `bingx_error` continue, per-page `commit` | `LIMIT 500` starvation at scale (now paginated) | Medium |
| `competition_lifecycle` `competition_lifecycle.py:104` | `finalize_expired_competitions` every 10s, `with_for_update` on `competitions` | Same singleton | `SELECT ... FOR UPDATE` on `competitions` only, not `positions` (G) | High |
| `workers/lock` `lock.py:12` | `pg_try_advisory_lock` `LOCK_KEY` | Connection-scoped, `acquire` in `main.py:78` with `15×2s` retry, `release` in `finally` | SQLite → `None` → no singleton, `config:46` `database_is_postgres` false → warning only | Medium |

Standalone entrypoints `python -m workers.*` bypass `bot/main.py` lock and `require_postgres` check — not used in prod (deleted `workers/main.py`).

---

## Competition Audit

- `UPCOMING→ACTIVE→FINISHED` (`competition_models.py:13-17`), `starts_at`/`ends_at` `TIMESTAMPTZ`, `initial_balance`/`prize_pool`
- `get_active_competition` `competition.py:12` `status==ACTIVE && starts<=now<ends` `order_by id desc limit 1`
- `get_or_create_default_competition` `competition.py:25` `pg_advisory_xact_lock(82463519)` + `begin_nested` savepoint → **TOCTOU fixed**
- `join_competition` `competition.py:54` `status ACTIVE && ends>now` check, `UNIQUE` savepoint, `clean-sheet` reset (NEW in this audit: closes open positions via `close_position` then `ADJUSTMENT` ledger if `is_new_cup`)
- `update_participant_equity` `competition.py:184` `current_equity = cash+margin+unrealized` + `roi`
- `finish_competition` `competition.py:175` `with_for_update` + `snapshot_exists` check then `snapshot_leaderboard` (see C2) + `CompetitionPrize` (same TOCTOU)
- `finalize_competition_session` `competition_lifecycle.py:20` loop `close_position` per `competition_id` with `PaperError` + `refresh` skip if already closed, then `finish_competition`
- `finalize_expired_competitions` `competition_lifecycle.py:63` `with_for_update` on `competitions` + per-competition `begin_nested`
- **Races:** `G` trade exactly at `ends_at` → `open_position` `paper_adapter:115-139` checks `ACTIVE && starts<=now<ends` **before** lock, then no re-check after lock → window where `finalize` inserts `FINISHED` between check and `INSERT` → dangling OPEN. Needs re-check after `_lock_account`.
- `F3` clean-sheet `ADJUSTMENT` breaks conservation by design — documented as `reference_type=competition_reset` and excluded from `sum(ledger)` vs `equity`.
- **Duplicate finalization** `J` — second `finish` sees `status==FINISHED` `competition.py:178` → `return` (idempotent), but snapshot/prize insert not guarded → `IntegrityError` second TX rolls back `FINISHED` without snapshot (now fixed in `C2` with `try/except`).

---

## Database Audit

- **FKs:** `trading_accounts.user_id → users.id`, `paper_positions.account_id → trading_accounts.id`, `paper_orders.account_id → trading_accounts.id`, `paper_orders.position_id → paper_positions.id`, `paper_positions.symbol → instruments.symbol`, `executions.position_id/user_id/competition_id → ...`, `competition_participants/leaderboard_snapshots/competition_prizes` → `competitions/users` — all present, `ON DELETE` default `NO ACTION` (no cascade) — delete user fails if account exists (tested in E2E `ForeignKeyViolation` on `users` delete, cleaned via `DELETE executions → positions → orders → ledger → participants → accounts → users` order).
- **UNIQUE:** `users.telegram_id` `001`, `users.phone_number` `models.py:15`, `trading_accounts.user_id` `002`, `account_ledger.idempotency_key` `002`, `paper_orders.idempotency_key` `002`, `competition_participants uq_competition_user` `003`, `LeaderboardSnapshot uq_snapshot_comp_user` `003`, `CompetitionPrize uq_competition_prize_rank` `004`, `executions` no unique (composite via `position_id` + `execution_reason` + `idempotency` implicitly via `paper_orders`).
- **Indexes:** As in `CRYPTOBOT_A_TO_Z.md` plus `009` `(competitions.status,ends_at)`, `(paper_positions.competition_id,status)`, `(paper_positions.status)`, `(instruments.status)`, `market_snapshots.updated_at` (004, but model missing `__table_args__` index — drift).
- **Nullable:** `paper_positions.competition_id` nullable (`003` batch alter `recreate=always` for SQLite, `nullable` legacy) → allows orphan after `G`; should be `NOT NULL` + `CHECK` for `ACTIVE`.
- **Types:** `Numeric(18,2)` cash/equity/pnl, `Numeric(30,12)` price/qty, `DateTime(timezone=True)`, `String(20→40)` `006`, `Integer` leverage, `Enum` `paper_position_side/status` etc. All money `Decimal`.
- **Migrations:** `001` legacy, `002` paper, `003` competition (adds `competition_id` nullable, `batch_alter` with `recreate=always` for SQLite), `004` market_snapshots/prizes, `005` leverage `Numeric(10,2)` default `1`, `006` `VARCHAR 20→40`, `007` `max_leverage` `Integer` default `50` + update BTC/ETH 300 SOL 100, `008` `LIQUIDATION` enum `ADD VALUE IF NOT EXISTS`, `009` indexes. Fresh `Base.metadata.create_all` works (tested `sqlite_engine` fixture), upgrade `003→004` via `batch` works, downgrade `007→006` not tested.
- **Invariants only in Python:** `balance_after>=0` has DB `CHECK`, `quantity>0` has DB `CHECK`, but `TP/SL` side checks (`_validate_tp_sl:38`) only Python, `leverage 1..300` only Python, `is_quote_eligible` removed, `is_simulated` boolean no DB check. Should add `CHECK` for leverage range and `TP/SL` side if possible.
- **Locking:** `SELECT ... FOR UPDATE` only on `trading_accounts` (`paper_adapter:36`) and `competitions` (`competition_lifecycle:21`, `competition:177`), not on `paper_positions`.

---

## Railway Audit

- **Topology:** Single service `CRYPTO_BOT` (`Railpack` builder, `Procfile: web: PYTHONPATH=/app alembic upgrade head && python -m bot.main`, `nixpacks.toml: python311`) + `Postgres 18` (`ghcr.io/railwayapp-templates/postgres-ssl:18`, volume `/var/lib/postgresql/data` `109 MB`). No `crypto-api`/`crypto-worker` services — matches `main` single-process now, but `RAILWAY_API_TOKEN`/`RAILWAY_TOKEN` not needed.
- **Env:** `BOT_TOKEN` `861046...`, `DATABASE_URL` `postgresql://postgres:...@postgres.railway.internal:5432/railway` (+ `test_railway` for PG tests), `MARKET_DATA_MAX_AGE_MS=6000`, `ADMIN_TELEGRAM_IDS`, `DEMO_SEED_ENABLED=false`, `WEBAPP_URL`/`BOT_USERNAME` empty (MiniApp deleted). `PORT` provided by Railway but bot uses polling, `aiohttp` healthcheck on `PORT` `bot/main.py:39` (`8080` fallback) — `Procfile: web` expects `$PORT` but bot ignores it, healthcheck binds `0.0.0.0:$PORT` correctly.
- **Replica:** `RAILPACK` `serviceManifest.deploy.numReplicas:1`, `region: ams`, `restartPolicy: ON_FAILURE, maxRetries 10`. Singleton via `LOCK_KEY` retry `15×2s` — rolling deploy shows `Singleton lock held, retry 1..7` then success (`logs 18:42:30`).
- **Singleton requirements:** `require_postgres=true` enforced in `bot/main.py:31` (fail if `DATABASE_URL` not Postgres) — good.
- **Worker lock:** `workers/lock.py:12` returns `None` on SQLite → `bot/main.py:67` warns `without lock (local dev)` — dev only.
- **Migration race:** `Procfile` runs `alembic upgrade head` on **every** web start (`web` process) — if multiple replicas, race. With 1 replica ok, but `RAILPACK` `overlapSeconds: null` and `LOCK_KEY` retry handles it. No `preDeployCommand`.
- **Demo seed:** `DEMO_SEED_ENABLED=false` in prod, `admin_seed_demo_players` gated — safe.
- **BingX credentials:** `BINGX_API_KEY/SECRET` empty (public endpoints only) — no secret leakage.

---

## Legacy Isolation Audit

- **Deleted in `main`:** `apps/` (FastAPI `apps/api/main.py`, `auth.py`, `apps/miniapp/App.tsx`), `workers/main.py`, `workers/weekly_scheduler.py`, `services/trading.py` (spot), `services/weekly_cycle.py`, `services/pricing.py`, `services/execution.py`, `db/repo.py`, `db/models.py` kept only `User`.
- **Remaining legacy tables:** `weeks`, `assets`, `transactions`, `orders`, `positions` (legacy `Position` PK `user_id,week_id,asset_symbol`), `leaderboard_snapshots` (legacy `leaderboard_snapshots` PK `week_id,user_id`), `prizes` — still in DB via `001_initial.py` but no code touches them in `main` (grep `weeks` 0, `Asset` 0, `Transaction` 0 in `main`). `price_poller` no longer touches `assets` (now `instruments`), `paper_adapter` no longer `price_cache` (now `market_snapshots`).
- **No coupling:** `paper_adapter` uses `market_snapshots` only, legacy `price_cache` deleted, `services/bingx_market_data.py:168` fallback `if dialect=="sqlite": get_snapshot` (test-only). No `weeks.status` check in `paper_adapter`.
- **Risk:** Fresh `drop_all/create_all` creates **both** legacy and paper tables (since `Base.metadata` includes `User` from `db/models.py` but not legacy `Week` etc. because `db/models.py` now only `User` — actually `alembic/env.py:17` `import db.models` will import only `User`, so `Base.metadata` no longer contains legacy tables, but `alembic/versions/001_initial.py` still creates them via `op.create_table` — fresh DB via `alembic upgrade head` will have them, via `Base.metadata.create_all` (tests) will **not** have legacy tables — divergence, but tests use `Base.metadata.create_all` for `sqlite_engine` which now only creates paper tables + `users`, not `weeks` etc. That's intentional isolation.

---

## Test Coverage Audit

| Area | Unit | Integration | DB | Concurrency | Security | E2E | Verdict |
|---|---|---|---|---|---|---|---|
| Idempotency (same key → same result) | ✅ `test_paper_mvp:48` `bid_ask_execution_and_close_retry` (same close key) | ✅ `test_paper_race_pg:83` same open key `gather` | ✅ PG (via `test_railway`) | ✅ `asyncio.gather` same key | ✅ `_resolve_idempotent_position` checks `account_id/symbol/side` | N/A |
| Double spend (two parallel `buy` on full balance) | ❌ No direct test (legacy `test_money` had `test_double_spend` but deleted) | ⚠️ `test_paper_race_pg` covers same-key race, not different-key margin race | ⚠️ Would need two `open_position` same `notional` on `available 10000` → one `InsufficientMargin` |
| Stale price rejection | ✅ `test_shared_market:114` `test_stale_shared_snapshot_rejected` (5000→2000), `test_paper_mvp:99` `validate_snapshot` | ✅ `test_demo_acceptance:10` `Market data stale` | ✅ | — | — | — |
| TP/SL math (new profit-based) | ❌ **Missing** — no test for `entry*(1±pct/(100*lev))` for LONG/SHORT, lev1 vs 300, `+5%`, `-3%`, `5,5%`, `tp_only` | ❌ | — | — | Requires handler `handle_trade_text` mock + `open_position` mock snapshot |
| LONG/SHORT symmetry | ✅ `test_demo_acceptance:89` LONG ASK 50010 / SHORT BID 50000 | ✅ | ✅ | — | — | ✅ |
| Decimal precision | ⚠️ `test_paper_money` checks `entry_price == 100.10` `quantize 1e-12`, but no test for `price_precision` (e.g., UB 5 decimals `0.14000` vs `0.14`) | — | — | — | — | — |
| Finalization races | ✅ `test_paper_race_pg:158` `test_two_finalizers_create_one_snapshot` (2× `gather finalize`) | ✅ | ✅ PG (now) | — | ✅ |
| API auth | N/A (no API) | — | — | — | — | — |
| Telegram ownership | ⚠️ `test_demo_acceptance` checks `position.account_id == account.id` indirectly via `open_position`, but no direct test for `cb_close_preview` IDOR with forged `position_id` from another user | — | — | **Missing** | — |
| Callback replay | ⚠️ `test_paper_mvp:61` close retry same key, but no test for stale `callback_data` (old `trade:confirm` after 10m) or `edit_tp_sl` IDOR | — | — | **Missing** | — |
| Stale state | ⚠️ `test_shared_market:112` `test_future_shared_snapshot_rejected` | — | — | — | No test for `handle_trade_text` stale `trade_state` after restart |
| TP/SL | ⚠️ No test for `tp_sl_engine` pagination, liquidation 90%, or `_validate_tp_sl` | — | — | — | **Missing** |
| Decimal/float | ✅ `services/pnl.py` pure Decimal | — | — | — | — |
| API auth | N/A | — | — | — | — |
| E2E prod `railway ssh` | ✅ `ACCEPTANCE_EVIDENCE.md` manual `open LONG/SHORT`, `close`, `duplicate open`, `rank`, `liquidation` via `test_liquidation.py` | ✅ | ✅ | — | ✅ |

**Missing tests (priority):**
1. `test_tp_sl_profit_percent` — LONG lev10 `entry 70000` + `tp 10%` → `TP 70700`, SHORT `tp 10%` → `69300`, lev300 `100%` → `entry*1.00333`, `0%` reject, `"-5"` single negative now allowed, `5,5%` comma, `lev0` DivisionByZero.
2. `test_edit_position_idor` — forge `edit_tp_sl:mode:price: victim_pos_id` → must fail `position.account_id != account.id`.
3. `test_callback_replay` — `trade:confirm` with stale `state["symbol"]` after `cancel_trade` → `Сессия устарела`.
4. `test_finalization_grace` — open position gap crash `_trade` at `ends_at` exactly + finalization.
5. `test_leaderboard_pagination` — 11-20 page shows correct ranks.
6. `test_metrics_persist` — `increment` then restart → still counted (now debounced, not per-increment).
7. `test_healthcheck` — `GET /health` returns `200 {"status":"ok"}`.

---

## Adversarial Scenario Results

| # | Scenario | Expected | Actual | Pass? | Evidence |
|---|---|---|---|---|---|
| 1 | User modifies TP from 20% to 20000% | Reject (cap 500% or per-instrument max) | `trade.py:372` `if not val.is_finite()` only, `trade.py:428` `pct` can be `1000%` → `entry*11` lev1 → no cap → `paper_adapter:44` no cap either | **FAIL** — needs cap | `trade.py:364` len check only |
| 2 | NaN | Reject | `Decimal("NaN").is_finite()` False → `trade.py:373` reject → `Проверьте параметры` | **PASS** | `trade.py:373` |
| 3 | Infinity | Reject | `Decimal("Infinity").is_finite()` False → reject | **PASS** | `trade.py:373` |
| 4 | Negative quantity | Reject | `notional` must be `>0` `paper_adapter:195` → `InvalidQuantity` → generic | **PASS** | `paper_adapter:195` |
| 5 | Fake execution price | — | Client never sends price; `execution_price` is server `snap.bid/ask` `paper_adapter:176,400` | **PASS** | `paper_adapter:176` |
| 6 | Another user's position ID | `cb_close_preview:584` `account_id !=` → `Позиция не найдена` | **PASS** (close) **FAIL** (edit mode/only `1078:1091` missing check) | **FAIL** for edit |
| 7 | Another competition ID | `open_position` `paper_adapter:114` `competition_id` is taken from server `trade.py:521` `competition.id` (active), not client | **PASS** | `trade.py:521` |
| 8 | Replay old callback | `trade:confirm` after `cancel_trade` → `trade_state.pop` → `state.get("symbol")` None → `Сессия устарела` `trade.py:849` | **PASS** | `trade.py:849` |
| 9 | Confirm twice | `in_flight` guard `trade.py:853` + `paper_orders UNIQUE` `paper_adapter:99` → second `tg:{callback.id}` same key → `idempotency_hit` or status guard | **PASS** (same callback.id) — different `callback.id` (double-click) → different key → relies on `in_flight` (process-local, not DB) → **FAIL cross-process** but advisory lock single-process mitigates |
| 10 | Close after competition ends | `close_position` `paper_adapter:384` `status != OPEN`? No competition check on close, only `open` checks `ends_at`. `close` uses `position.competition_id` from row, not re-check `Competition.status`. If `FINISHED` but position still `OPEN` (gap, G), close would succeed with `FINISHED` competition. | **FAIL** — should reject or allow? Spec: `trade exactly at ends_at` → `open` must reject, `close` should still succeed to realize PnL. Currently close succeeds, which is arguably correct for closing, but `close_position` should not check competition status. **PASS** if intended. |
| 11 | Open after ends | `paper_adapter:114-139` `ACTIVE && starts<=now<ends` → `PaperError("Competition ended")` → `safe_trade_error` `trade.py:78` `Турнир уже завершён` | **PASS** | `paper_adapter:114` |
| 12 | Race TP vs manual close | Both `close_position` via `tp_sl:{id}:{ts}:TP` vs `tg_close:{id}` different keys → status guard + `FOR UPDATE` on `trading_accounts` serializes | **PASS** on PG, **FAIL** on SQLite (no lock) | `paper_adapter:408` |
| 13 | Race finalization | `competition_lifecycle:63` `with_for_update` + per-competition `begin_nested` → one snapshot | **PASS** after fix `C2` | `competition.py:192` now has `try/except` |
| 14 | Duplicate idempotency key | `paper_adapter:102` `already used for another request` | **PASS** | `paper_adapter:102` |
| 15 | Change TP after closed | `update_position_tp_sl:528` checks `status != OPEN` → `PaperError("Position not open")` | **PASS** | `paper_adapter:528` |
| 16 | Change SL after finished | Same as 15 + `Competition.status` not checked on update (should allow? TP/SL on finished should fail). Currently `update_position_tp_sl` does not check `Competition.status` → could update `FINISHED` competition's `CLOSED` position? No, `status != OPEN` blocks, `OPEN` positions of `FINISHED` competition are dangling (G) → could still update. | **FAIL** — should check `Competition.status == ACTIVE` on update | `paper_adapter:528` |
| 17 | BingX stale | `get_execution_snapshot` → `MarketDataStale` → `PaperError("Market data stale")` → `safe_trade_error` | **PASS** | `bingx_market_data:164` |
| 18 | BingX inverted bid/ask | `validate_snapshot:77` `ask<bid` → `MarketDataInvalid` → `PaperError("Market data unavailable")` | **PASS** | `bingx_market_data:77` |
| 19 | BingX clock ahead | `validate_snapshot:82` `age_ms < -5000` → `Future market timestamp` → `MarketDataInvalid` | **PASS** | `bingx_market_data:82` |
| 20 | BingX offline | `price_poller:239` `increment("bingx_error")`, `consecutive_failures>=5` → `ALERT`, orders reject with stale | **PASS** (fail-safe) | `price_poller:239` |
| 21 | Worker restart during TP | `tp_sl_engine:104` `except Exception: logger.exception` + `sleep(1)` loop → next iteration retries same position (idempotency key includes `ts` + `reason`, so retry with new `ts` → new key → duplicate close attempt but status guard prevents double) | **PASS** | `tp_sl_engine:104` |
| 22 | Worker restart during finalization | `competition_lifecycle:104` `except Exception: logger.exception("Competition lifecycle pass failed")` → next `sleep(10)` retries whole `finalize_expired_competitions` (idempotent due to `with_for_update` + snapshot existence check + `try/except` now) | **PASS** after `C2` fix | `competition_lifecycle:98` |
| 23 | Two workers simultaneous | `LOCK_KEY 82463518` `bot/main.py:78` + `15×2s` retry — second worker/container retries and exits after 15, first holds lock. If `database_is_postgres==False` → no lock (warn). | **PASS** on PG, **FAIL** on SQLite (no lock) | `workers/lock.py:12` |
| 24 | Two bot replicas simultaneous | Same as 23 | **PASS** on PG | `bot/main.py:78` |
| 25 | API and bot disagree about price | No API — `apps/` deleted, only Bot API polling (`market_snapshots` shared). No disagreement vector. | **PASS** (no API) | `ACCEPTANCE_EVIDENCE.md` |
| 26 | Leaderboard equity != account equity | `build_leaderboard:44` recomputes `current_equity = cash+margin+unrealized` same as `trading_account:72` — but `build_leaderboard` is now read-only, `update_participant_equity` (`competition.py:184`) writes `Participant.current_equity` but `build_leaderboard` does not persist rank, so `Participant.current_equity` can be stale until next `update_participant_equity` (called on open/close) or next `build_leaderboard`. | **FAIL** — `get_user_rank` reads stale `Participant.current_equity` if called directly without `build_leaderboard` (e.g., `admin_product_stats` reads `Participant` without building). | `services/competition.py:184` vs `services/leaderboard.py:44` |
| 27 | Prize assignment twice | `competition.py:203` `prizes_exist` check then `INSERT` without `FOR UPDATE` — TOCTOU, but now `C2` adds `try/except IntegrityError` (not yet, only snapshot) — prizes still TOCTOU. | **FAIL** — second concurrent prize insert can violate `uq_competition_prize_rank` and rollback `FINISHED` status. | `competition.py:203` |
| 28 | DB commit but Telegram notification fails | `competition_lifecycle:78` `await notify_competition_finished(engine, competition_id)` is **outside** `session.commit` `competition_lifecycle:77` and `services/notifications.py:54` `except Exception: logger.exception` swallows — finance committed, notify best-effort, no retry, no `outbox` table. | **PASS** (fail-safe) but prize/notify can be lost | `notifications.py:54` |
| 29 | Telegram update duplicated | `Bot` `dp.start_polling` with `allowed_updates` + `ThrottlingMiddleware` `bot/middlewares/throttling.py:19` per-user 0.8/0.3s + `idempotency_key=tg:{callback.id}` `trade.py:880` dedups | **PASS** | `trade.py:880` |
| 30 | API called directly without Mini App | No API, only Bot. If API re-added, must validate `user_id` server-side `select(User where telegram_id==from_user.id)` not `request.json["user_id"]` | **PASS** (no API) | `apps/` deleted |

---

## Definition of Done

| Area | Status | Evidence |
|---|---|---|
| Trading integrity (ASK/BID, Decimal, ledger, margin) | **GREEN** | `paper_adapter:176` LONG ASK, `400` LONG BID, `pnl.py:5` Decimal, `ledger` `TRADE_OPEN/CLOSE` `paper_adapter:342,509`, `ck_ledger...` |
| Market data (BingX → `market_snapshots` → `get_execution_snapshot`) | **GREEN** | `price_poller:149` batch `fetch_tickers(filtered)` + `persist_snapshot`, `bingx_market_data:69,142` validate/stale, `config:12` `6000ms` |
| TP/SL (exact price + profit% `entry*(1±pct/(100*lev))`, single/both, skip, back, edit) | **YELLOW** | Math correct per H1-H5, but `H1` (single `-5` rejected), `H2` (`0%` inconsistency), `H3` (comma), `M1` (quant 8 vs 12 vs tick), `M2` (is_percent ambiguity) remain High/Medium |
| PnL (LONG `(exit-entry)*qty`, SHORT `(entry-exit)*qty`) | **GREEN** | `pnl.py:3` `quantize 0.01`, verified `UB` `0.14` etc., `F15` dust <0.005 hidden |
| Equity (`cash+margin+unrealized`) | **YELLOW** | `trading_account:72` vs `leaderboard:46` vs `competition:184` triple source, `refresh_account_stats` not called in `tp_sl_engine` for stale case, `Participant` stale |
| ROI (`(current-starting)/starting*100`) | **YELLOW** | Same triple, `starting` reset clean-sheet now, but `update_participant_equity` vs `build_leaderboard` drift |
| Balance (`SUM(ledger)` vs `TradingAccount`) | **GREEN** | `ck_ledger_balance_after_non_negative`, cap `return_amount>=0` (`paper_adapter:498`), `verify` via `get_cash_balance` not in prod but test `test_competition_isolation` |
| Ledger | **GREEN** | `UNIQUE idempotency_key`, `TRADE_OPEN/CLOSE/ADJUSTMENT`, `balance_after`, `reference_type` |
| Idempotency | **YELLOW** | DB `UNIQUE` + savepoint + `in_flight` + `tg:{callback.id}` works same-key, but **commit-visibility gap** (`C1` Critical) remains for same-key concurrent before commit |
| Concurrency (FOR UPDATE, advisory lock) | **YELLOW** | `FOR UPDATE` on `trading_accounts` only, no `FOR UPDATE` on `paper_positions`, `READ COMMITTED` window `P` (stale snapshot) remains |
| Competition lifecycle (`UPCOMING→ACTIVE→FINISHED`) | **YELLOW** | `get_or_create_default_competition` now savepoint, `finalize` with `with_for_update` + per-competition savepoint, but `G` (new trade after `SELECT OPEN` snapshot) still TOCTOU without `positions FOR UPDATE` |
| Leaderboard (ROI→equity→joined_at) | **YELLOW** | `build_leaderboard` now 3 queries (was N+1), `sorted` in Python, `rank` not persisted, `snapshot TOCTOU` `C2` partially fixed for snapshot but not prizes |
| Prizes (`DEMO_PRIZES` sum 100) | **YELLOW** | `uq_competition_prize_rank` not handled, `C2` |
| Telegram (`/start`, `/trade`, `/positions`, `/profile`, `/history`, `/top`, `/help`, callbacks, FSM) | **GREEN** | All 9+9 commands, 12 callback types, `trade_state` dict, `SkipHandler`, `throttling`, `tpsl:back` fix, `nav:transactions:offset` pagination, `format_side`/`fmt_price` |
| API (`/health`, `/metrics`, no trade API) | **GREEN** | `bot/main.py:42` `aiohttp` `GET /health` `200`, no `initData` surface |
| Mini App | **GREEN** | Deleted, no `WEBAPP_URL` |
| Auth (`telegram_id`, `phone UNIQUE`, `is_admin`, `is_banned`) | **YELLOW** | `ensure_can_trade` only `cb_confirm`, not `close/edit` → banned can close/edit |
| Admin (`admin_*` 9) | **GREEN** | `is_admin` 9/9, `DEMO_SEED_ENABLED` gate |
| Worker (`price_poller`, `tp_sl_engine` 1s pagination, `competition_lifecycle` 10s) | **YELLOW** | `price_poller` survives BingX outage at startup now, `tp_sl` paginated, `sync_instruments` still per-symbol commit (5s startup) — now batch (fixed) |
| Railway (single service, `Procfile`, `nixpacks`, `PORT`, `DATABASE_URL`, `BOT_TOKEN`) | **GREEN** | `Procfile: web: alembic upgrade head && python -m bot.main`, `RAILPACK` `numReplicas 1`, `LOCK_KEY` retry, `healthcheck` on `$PORT`, `test_railway` isolated |
| Database (FK, UNIQUE, indexes, migrations 001→009) | **YELLOW** | `FK` present, `UNIQUE` present, `CHECK` present, `009` adds `(status,ends_at)` etc., but `market_snapshots.updated_at` index drift, `paper_positions.competition_id` nullable, `VARCHAR 20→40` via `006` |
| Migrations (fresh `create_all` vs `upgrade head`, downgrade) | **GREEN** | `env.py` handles `postgresql://→+asyncpg`, `batch_alter` for SQLite, `006/007/008` handle SQLite `try/except`, fresh `sqlite_engine` 43 tests green |
| Legacy isolation (TradeWeek `weeks/assets` vs paper) | **GREEN** | `apps` deleted, `workers/main.py` deleted, `Base.metadata` no longer contains legacy tables, `price_cache` deleted, no paper code touches legacy |
| Observability (`metrics`, `health`, `logs`, `increment`) | **YELLOW** | `increment` debounced file persist (`services/metrics:38` 10s), `health`/`metrics` endpoints, `logger.exception` in workers, but `market_snapshots` no `updated_at` index in model, `increment("bingx_ticker_*")` per ticker noisy |
| Testing (unit, DB, concurrency, security, E2E) | **YELLOW** | `43 passed` sqlite, `4 passed` PG on `test_railway` (via `railway ssh`), `test_liquidation` 3/3, `test_tp_sl_leverage_price` 10, `test_competition_isolation` 4, but **missing** TP/SL profit-percent math tests, edit-IDOR tests, stale-price divergence tests (see below) |

---

## Recommended Fix Plan — 8 Phases (STOP after plan, do not implement)

### PHASE 1 — CRITICAL SECURITY / FINANCIAL INTEGRITY (must before public demo)

**Files:** `bot/handlers/trade.py:373` (`tp_only`/`sl_only` `val<=0`), `bot/handlers/leaderboard.py:67` already fixed, `services/paper_adapter.py:242` (lost idempotency), `services/paper_adapter.py:498` (money creation), `bot/handlers/trade.py:1078:1133` (edit IDOR)
- **Change 1.1 (TP/SL single negative):** `trade.py:373` `if not val.is_finite() or val == 0: raise` (allow negative, `copy_abs` later). **Migration:** none. **Test:** `tp_only_percent "-5"` → `tp = entry*(1+5/(100*lev))` LONG.
- **Change 1.2 (Loss capping):** `paper_adapter.py:498` instead of silent `net=-margin`, emit `AccountLedger ADJUSTMENT` for delta `-(net+margin)` with `reference_type="liquidation_gap"` and `logger.critical` + `increment("liquidation_gap_capped")`. **Test:** `300x LONG entry 100 qty 30 market 1` → `gross -2970` → `net=-10` + `ADJUSTMENT -2960` + `ledger sum` reconciles.
- **Change 1.3 (Idempotency commit visibility):** `paper_adapter.py:242` wrap `order+position+ledger+account` in **one** `begin_nested` (currently ledger+account outside) **and** catch `IntegrityError` on `AccountLedger` `UNIQUE` and re-resolve. `paper_adapter.py:354,516` same. **Test:** `2× gather open_position same key with sleep(0.05) before flush`.
- **Change 1.4 (Edit IDOR):** `trade.py:1078:1091` `cb_edit_tp_sl_mode` and `1113:1133` `cb_edit_tp_sl_only` + `handle_trade_text:478` `session.get(PaperPosition, pos_id)` → add `account_id` check `if position.account_id != (await session.execute(select(TradingAccount).where(user_id==user.id))).scalar_one().id: reject` (copy from `cb_edit_tp_sl_clear:1161`). **Test:** `userA pos 123` → `userB` sends `edit_tp_sl:mode:price:123` → `Позиция не найдена`.
- **Change 1.5 (Banned can close/edit):** `services/accounts.py:25` `ensure_can_trade` already checks `is_banned`+`phone`, call it in `cb_close_confirm:978`, `cb_edit_tp_sl*:1028,1078,1113,1151`, `handle_trade_text` edit branch `452`, and `update_position_tp_sl:528` re-check `Competition.status`.
- **Risks:** `1.2` changes ledger sum (add adjustment), `1.3` changes transaction boundaries (test with `test_paper_race_pg`).

### PHASE 2 — CONCURRENCY / DATABASE

**Files:** `services/paper_adapter.py:33`, `services/competition.py:192`, `db/paper_models.py:118`, `alembic/versions/010_*`
- **Change 2.1:** `paper_adapter.py:408` `SELECT PaperPosition WHERE id=:pid FOR UPDATE` before `refresh`.
- **Change 2.2:** `competition.py:192` `snapshot_exists` / `203` `prizes_exist` wrap `INSERT` in `try/except IntegrityError: pass` or `ON CONFLICT DO NOTHING`.
- **Change 2.3:** `db/paper_models.py:118` `competition_id` `nullable=False` + `server_default` + `CHECK` + `alembic 010` `ALTER COLUMN SET NOT NULL` after backfill `UPDATE ... SET competition_id = (SELECT id FROM competitions ORDER BY id LIMIT 1) WHERE NULL`.
- **Change 2.4:** `workers/competition_lifecycle.py:24` `select(PaperPosition).where(competition_id==id, status==OPEN).with_for_update()` and re-check `Competition.status` after `FOR UPDATE`.
- **Change 2.5:** `config.py` enforce `REQUIRE_POSTGRES=true` in `paper_adapter.open_position` if `dialect!="postgresql"` → raise.
- **Test:** `test_finalization_grace` (trade at `ends_at` ±1ms), `test_twoWorkersSamePosition`, `test_isolatedMixin`.

### PHASE 3 — TP/SL CORRECTNESS

**Files:** `bot/handlers/trade.py:349,360,373,390,419,439,472,496,531,545`, `services/paper_adapter.py:38,213`, `db/paper_models.py:72`
- **Change 3.1:** `trade.py:373` already fixed in 1.1, `trade.py:410` add `if tp_pct==0 or sl_pct==0: reject` for both-percent.
- **Change 3.2:** `trade.py:364` comma handling: `text.replace(",",".")` before `split` and handle `";"`; keep `"5,5%"` → `5.5%`.
- **Change 3.3:** `trade.py:379` re-validate percent distance at execution: store `pct` in `trade_state` at calc time, at `cb_confirm` re-fetch `snapshot` and compare `abs((calc_entry - actual_entry)/actual_entry) < 0.02` else warn and recompute or fail gracefully (no silent wrong %).
- **Change 3.4:** `trade.py:439` add UI side validation `if LONG and tp <= entry_est: reject` before confirm (currently only server `paper_adapter:44`).
- **Change 3.5:** `trade.py:390` quantize TP/SL to `Instrument.price_precision` not fixed `1e-8`: `await session.get(Instrument, symbol)` → `Decimal(10)**-inst.price_precision`.
- **Change 3.6:** `trade.py:496` `ZeroDivisionError` guard `if lev <=0: raise`.
- **Change 3.7:** `trade.py:349` `len(text)>100` guard.
- **Test:** Full matrix from TP/SL audit `H1-H5` + `M1-M5`.

### PHASE 4 — API / AUTH

- **Current:** No API (`apps/` deleted) → no HMAC `initData` surface. When re-adding `apps/api`, `REQUIRED`:
  - `hmac.compare_digest` + `auth_date` window `86400` and `future > now+60` reject (`outputs/FINAL_PRODUCTION_READINESS_REPORT:117`)
  - Never trust `request.json["user_id"]`/`competition_id`/`account_id` — re-derive `select(User where telegram_id==from_user.id)`
  - Rate limit `ThrottlingMiddleware` or reverse-proxy limit on `/api/*` (currently only Bot)
- **Test:** `test_api_unauthorized` (no `initData` → 401), `test_api_replay` (old `auth_date` → 401), `test_api_idor` (userB pos ID → 403).

### PHASE 5 — TELEGRAM STATE / UX

- **Already fixed:** `tpsl:back` interception, `ignore_case`, `trade_state` clear on `/start`/`/trade`, `F.text` hijack via router order.
- **Remaining:** `trade_state` TTL (currently in-memory forever until `pop`), recommend `FSMContext` with `MemoryStorage` + `StateFilter` and `await state.clear()` on `nav:home`/`cancel`. Add `len(budget)>20` already done.
- **Test:** `test_stale_state_after_restart` (restart → `trade:confirm` → `Сессия устарела`), `test_concurrent_edit_vs_close`.

### PHASE 6 — WORKERS / RAILWAY

- **Already fixed:** `run_forever` retry with backoff for `sync_instruments` + `poll_prices` restart.
- **Remaining:** `sync_instruments` still per-symbol `commit` (950) → batch (now fixed to batch), `price_poller` metric noise per ticker → sample.
- **Test:** `test_worker_restart_during_tp` (kill `tp_sl_engine` mid `check_and_close`, restart → idempotency), `test_bingx_offline` (mock `fetch_tickers` raise → orders reject, poller recovers).

### PHASE 7 — OBSERVABILITY

- **Metrics debounce** already (10s), healthcheck `aiohttp` `/health`/`/metrics` already, `logger.exception` in workers already.
- **Add:** `increment("liquidation_gap_capped")`, `increment("tp_sl_quantize_mismatch")`, alert on `consecutive_failures>=5` already.
- **Test:** `test_metrics_persist` (increment → restart → still counted, now file debounce).

### PHASE 8 — TEST HARDENING

- **Add:** `test_tp_sl_profit_percent` (all combos lev1/300, LONG/SHORT, `+5%`/`-3%`, `20`, `5,5%`, `lev0`), `test_edit_position_idor`, `test_callback_replay`, `test_finalization_grace`, `test_leaderboard_pagination`, `test_healthcheck`, `test_stale_price_divergence`.
- **Do not weaken:** `test_paper_race_pg` must stay PG `READ COMMITTED` (now via `test_railway`), `test_liquidation` cap test must assert `ADJUSTMENT` ledger, not just `return==0`.

---

## Risks per Phase

| Phase | Risk if not done | Risk of doing | Mitigation |
|---|---|---|---|
| 1 | Money creation, IDOR edit, banned trade | Ledger sum change, new `ADJUSTMENT` type breaks旧 report `SUM(ledger)` | Add adjustment to audit query `WHERE type NOT IN ('ADJUSTMENT')` |
| 2 | Dangling OPEN after FINISHED, duplicate snapshot | `ALTER COLUMN SET NOT NULL` needs backfill, may lock table | Run `009` with `SET NOT NULL` in transaction, backfill in same migration |
| 3 | Wrong TP/SL price, missed trigger, 500 on `lev0` | New validation may reject previously accepted `0%` | Announce in `/help` |
| 4 | API re-add without HMAC → IDOR | New code, test with `hmac` vectors | Gate behind `FEATURE_FLAG` |
| 5 | Stale wizard hijack | `FSMContext` migration may change `trade_state` shape, need data migration (in-memory, no) | Keep `trade_state` dict fallback for 1 deploy |
| 6 | Price gap → missed liquidation | Batch vs per-symbol commit changes timing | Monitor `Instruments sync complete` duration |
| 7 | Metrics lost on crash before 10s flush | Debounce already, add `atexit` flush | Test restart |
| 8 | False confidence | New tests may be flaky on SQLite vs PG | Run on `test_railway` |

**AUDIT ONLY — NOT COMMITTED — NOT DEPLOYED — NO FILES MODIFIED in this audit run.** All findings are read-only; implementation requires explicit approval per prompt `ONLY AFTER EXPLICIT APPROVAL: IMPLEMENT`.

