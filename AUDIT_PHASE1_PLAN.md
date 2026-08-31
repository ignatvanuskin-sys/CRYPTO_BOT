# PHASE 1 IMPLEMENTATION PLAN — Critical Fixes (AUDIT ONLY, NOT IMPLEMENTED)

**Status: PLAN ONLY — No files modified, no commits, no deploys in this phase (per authorization)**
**Source audit:** `AUDIT_DEEP_REPORT.md` (2026-08-30, `main` @ `55c377c`)
**Scope: PHASE 1 ONLY — 4 fixes (1 HIGH IDOR, 2 CRITICAL financial/concurrency, 1 HIGH UX)**

---

## Current State Snapshot (INSPECT)

**Branch:** `main` — 8 commits since `legacy/tradeweek-snapshot`, latest `55c377c` (TP/SL profit-based refactor), `08` liquidation migration applied on Railway (`test_railway` verified `4 passed`).

**Key files inspected:**
- `bot/handlers/trade.py:373` — single-percent `val <=0` rejects `-5`/`-5%`
- `services/paper_adapter.py:498` — `if return_amount<0: net=-margin; return=0` (silent cap, no ledger adjustment)
- `services/paper_adapter.py:242` — `order+position` inside `begin_nested`, `ledger+account` outside, `IntegrityError` not caught for ledger
- `bot/handlers/trade.py:1078,1113,478` — `session.get(PaperPosition, pos_id)` without `account_id` check

**Tests:** `43 passed, 4 skipped` (sqlite), `4 passed` on Railway `test_railway` (via `railway ssh` `pytest`), `3 passed` `test_liquidation`, `10 passed` `test_tp_sl_leverage_price`.

**Production:** Single service `bot/main.py` (`LOCK_KEY` 82463518, `15×2s` retry), `price_poller` `DEMO_WATCHLIST` 25, `MARKET_DATA_MAX_AGE_MS=6000`, `health`/`metrics` on `:8080`.

---

## Fix #1 — trade.py:373 Single Negative Percent (HIGH)

### Before
```python
# bot/handlers/trade.py:372
val = Decimal(parts[0])
if not val.is_finite() or val <= 0:  # rejects -5, -5%, +5% (after copy_abs would be 5)
    raise InvalidOperation
pct = val.copy_abs()  # would handle -5 → 5, but never reached
```

### After (Plan)
```python
val = Decimal(parts[0].replace("%","").replace(",","."))
if not val.is_finite() or val == 0:  # allow -5, +5, 5, 5%
    raise InvalidOperation
pct = val.copy_abs()  # magnitude only
# Preserve existing: is_tp_only vs is_sl_only decides direction, not sign
# "5 -3" → tp=5, sl=3 (both magnitudes), not tp=5, sl=-3
```

**Why:** Prompt says `5, +5, -5, 5%, +5%, -5%` must all mean `5%` (magnitude). Current `val<=0` rejects `-5` before `copy_abs`, contradicting prompt `"...через пробел, например: 5% -3%"` and handler's own `copy_abs` intent. `0%` should still be invalid (TP==entry).

**Files:** `bot/handlers/trade.py:373` (single) and `trade.py:488` (edit single) — change `val <=0` to `val ==0` (or `val.copy_abs()==0`). Keep `v1==0` check for both-percent at `trade.py:414` (add `tp_pct==0 or sl_pct==0`).

**Tests:**
- `"-5"` + `LONG` `lev10` `entry 100` → `TP 100*(1+0.05/10)=100.5` (not rejected)
- `"-5%"` → same
- `"5"` → `TP 100.5`
- `"0"` / `"0%"` → rejected `>0`
- `"5 -3"` → `tp 0.5%, sl 0.3%` (both positive magnitudes, sl below for LONG)
- `"-5 -3"` → `tp 5%, sl 3%` (sign ignored)
- LONG/SHORT × lev1/300 × `0.1%`, `5.5%` decimal

**Regression:** LONG/SHORT formulas unchanged `entry*(1 ± pct/(100*lev))`, `Decimal` only.

---

## Fix #2 — paper_adapter.py:498 Loss Capping Accounting (CRITICAL)

### Why cap exists
- `CHECK balance_after >=0` (`paper_models.py:91`) is DB invariant.
- Without cap, gap close (300x LONG `notional 3000` `margin 10` crash `100→1` → `net≈-2970`) → `return_amount = -2960` → `cash_balance` negative → `CHECK` violation → `DBAPIError` instead of business error.
- Leverage/margin **does not** prevent gap beyond liquidation buffer (90% in `tp_sl_engine:60`), gap `>10%` bypasses.

### Current (cap creates money)
```python
# 498-502
returned_margin = (notional/leverage).quantize(0.01)
return_amount = (margin + net).quantize(0.01)
if return_amount < 0:
    net = -returned_margin
    return_amount = Decimal("0.00")
    position.realized_pnl = net
# ... update account, ledger with return_amount=0, no adjustment for capped delta
```

**Problem:** `$2960` appears from nowhere (isolated-margin intent but violates `sum(ledger) == equity - initial`).

### After (Plan) — Explicit ADJUSTMENT, auditable, idempotent
```python
returned_margin = (notional/leverage).quantize(0.01)
return_amount = (margin + net).quantize(0.01)
capped_delta = Decimal("0")
if return_amount < 0:
    # Cap loss at margin, record exceptional gap as ADJUSTMENT
    capped_delta = return_amount  # negative, e.g. -2960
    net = -returned_margin
    return_amount = Decimal("0.00")
    position.realized_pnl = net
# ... account updates with capped net/return
# After ledger TRADE_CLOSE, if capped_delta !=0:
if capped_delta != 0:
    adj = AccountLedger(
        account_id=account.id,
        type=LedgerType.ADJUSTMENT.value,
        amount=capped_delta,  # negative
        balance_after=account.cash_balance,  # unchanged (return 0, so cash not moved by adjustment)
        reference_type="liquidation_gap",
        reference_id=str(position.id),
        idempotency_key=f"{idempotency_key}:gap_adj" if idempotency_key else f"gap:{position.id}",
    )
    session.add(adj)
    increment("liquidation_gap_capped")
    logger.critical("Gap capped %s for position %s", capped_delta, position.id)
```

**Invariants:** `ledger TRADE_CLOSE (0) + ADJUSTMENT (capped_delta)` sum = `net` with cap, `ledgerDerivedBalance = sum(ledger.amount)` still equals `account.cash_balance - initial`? Actually `initial + sum(ledger)` must equal `cash_balance` (since `ADJUSTMENT` is part of ledger). With `return 0`, `cash` unchanged by close, but `ADJUSTMENT` negative would make `cash` diverge. Better: `ADJUSTMENT` should **not** affect `cash` (it's just audit), or should be `0` amount with metadata. For conservation, `ADJUSTMENT` amount should be `0` and `capped_delta` stored in `metadata`? But prompt says "make adjustment amount explicit". We will make `ADJUSTMENT` with `amount = capped_delta` and `balance_after` same as before (so ledger sum includes capped loss, cash not moved). To keep `ledger sum == cash - initial`, we must **also** adjust `cash`? Actually `cash` is not moved by `ADJUSTMENT` if we set `balance_after` same. Ledger sum would then include `capped_delta` but cash wouldn't, breaking reconciliation.

**Correct model:** `ADJUSTMENT` **should** mutate `cash`? No, `cash` already reflects capped `return 0` (no credit). The exceptional gap loss beyond margin is not charged to cash (isolated margin), so ledger should **not** include it as cash movement; it should be an informational adjustment with `amount=0` and `metadata_json` containing `capped_delta`. But prompt says "make adjustment amount explicit" and "preserve double-entry".

**Decision:** Create `ADJUSTMENT` with `amount=0`, `balance_after=cash`, `metadata_json='{"capped_delta": "-2960.00", "gross": "-2970.00"}'` — auditable, does not affect `sum(ledger)` vs `cash`. Alternative: include `capped_delta` in `amount` and also adjust `cash` by same (so `cash` would go negative, violating CHECK). So we keep `amount=0` and audit via metadata.

**Files:** `services/paper_adapter.py:489-502`, `db/paper_models.py:78` (`metadata_json` already exists in `AuditLog` but not `AccountLedger`; `AccountLedger` has no metadata, but we can reuse `reference_type="liquidation_gap"` and `amount=0`).

**Test matrix:**
- CASE A normal loss: `entry 100, qty 10, margin 100, exit 99` → `net -10` → `return 90` → `cash 90`, no adjustment, `ledger sum == cash - initial` (10k→9990)
- CASE B loss == margin: `net -100` → `return 0` → `cash 0` after? Actually `cash` was 0 after open? Need trace: open `cash 10000→9900` (margin 100), close `return 0` → `cash 9900`, `realized -100`
- CASE C loss > margin: `net -200` → capped `net -100`, `return 0`, `ADJUSTMENT` with `capped_delta -100` metadata, `cash 9900`, `realized -100`, no negative `balance_after`
- CASE D retry same `idempotency_key` after capped close → `existing_order` early return `position.realized_pnl == -100` (capped), no second ledger
- CASE E reconciliation: `sum(ledger.amount for TRADE_OPEN/CLOSE) + initial == cash` and `sum(ledger where type != ADJUSTMENT) == realized+ (equity-initial)` — helper `assert_reconciliation(session, account)` checks.

**Migration:** None if `ADJUSTMENT` with `amount=0` (existing enum value `ADJUSTMENT` already exists `paper_models.py:26`). No schema change needed. If we need new `LedgerType` value, add migration `010` but `ADJUSTMENT` suffices.

---

## Fix #3 — paper_adapter.py:242 Idempotency Atomicity (CRITICAL)

### Current
```python
# 220-235
await _lock_account(session, account.id)
await session.refresh(account)
existing_after_lock = SELECT ... WHERE idempotency_key=:key  # 221
if existing_after_lock: return
notional = calc...
required_margin > available → begin_nested() INSERT REJECTED order → raise
# 265-355
try:
    async with session.begin_nested():
        INSERT PaperOrder FILLED
        INSERT PaperPosition
        INSERT Execution
except IntegrityError: return _resolve...
# Outside savepoint:
account.cash_balance -= margin  # 342
margin_used += margin
ledger TRADE_OPEN  # 346
refresh_account_stats
# No catch for ledger UNIQUE
```

**Problem:** Ledger+account outside savepoint, and second concurrent TX misses pre-lock SELECT (uncommitted), enters savepoint, gets `IntegrityError` on `PaperOrder` unique, but `_resolve` does `SELECT` and finds `None` (still uncommitted) → `raise PaperError("Idempotency key conflict")` instead of idempotent return. Also ledger `UNIQUE` not caught.

### After (Plan) — Single atomic savepoint for all financial writes

```python
await _lock_account(session, account.id)
await session.refresh(account)
# Re-check after lock (already exists at 221, keep)
existing_after_lock = ...
if existing_after_lock: ...

notional = ...
required_margin = ...

if required_margin > available:
    # REJECT path: must be atomic with ledger
    try:
        async with session.begin_nested():
            order = PaperOrder(..., status=REJECTED, ...)
            session.add(order); await session.flush()
            # No ledger for reject? Keep as is, but inside savepoint
    except IntegrityError:
        return await _resolve_idempotent_position(...)
    raise InsufficientMargin(...)

# SUCCESS path: all inside one savepoint
try:
    async with session.begin_nested():
        order = PaperOrder(FILLED, ...)
        session.add(order); await session.flush()
        position = PaperPosition(...)
        session.add(position); await session.flush()
        order.position_id = position.id
        execution = Execution(...)
        session.add(execution); await session.flush()
        # Account and ledger inside same savepoint
        account.cash_balance = (account.cash_balance - required_margin).quantize(0.01)
        account.margin_used = (account.margin_used + required_margin).quantize(0.01)
        ledger = AccountLedger(..., amount=-required_margin, balance_after=account.cash_balance, idempotency_key=f"{idempotency_key}:ledger")
        session.add(ledger)
        await session.flush()
        await refresh_account_stats(session, account)
except IntegrityError as e:
    # Could be PaperOrder or AccountLedger UNIQUE
    # Re-resolve: check if it's ledger collision vs order collision
    # For order collision, _resolve will find existing order
    # For ledger collision, same - ledger key is deterministic from order key, so if order exists, ledger also exists
    return await _resolve_idempotent_position(session, idempotency_key, account, symbol, side)
```

Same pattern for `close_position`: move `account.*` and `AccountLedger` (`509-517`) inside `try: async with session.begin_nested():` that already wraps `PaperOrder` (`420`), or create a new outer savepoint that includes both.

**Files:** `services/paper_adapter.py:242-360` (open), `420-525` (close)

**Test (PG-only, mark `pytest.mark.pg`):**
```python
@pytest.mark.asyncio
async def test_concurrent_open_same_key(pg_engine):
    # Use pg_engine fixture (real Postgres, not sqlite)
    # 2 concurrent open_position same idempotency_key, same account
    # Expected: 1 position, 1 execution, 1 ledger, second returns same position, balance mutated once
    # Assert: select count(*) from paper_positions ==1, executions==1, ledger==2 (INITIAL + 1 TRADE_OPEN), cash == 9900 (margin 100 deducted once)
```
Repeat for `close`. Add `pytest.skip` if `pg_engine` unavailable.

**Regression:** Existing `test_paper_race_pg` already covers same-key race but without ledger count assertion — extend to check `account_ledger` count and `cash_balance`.

---

## Fix #4 — IDOR Edit TP/SL (HIGH)

### Current (vulnerable)
```python
# trade.py:1078
pos = await session.get(PaperPosition, pos_id) # no account check
trade_state[uid] = {editing_position_id: pos_id, ...}

# trade.py:1113 (only)
pos = await session.get(PaperPosition, pos_id) # no check

# handle_trade_text:478
pos = await session.get(PaperPosition, pos_id)
if not pos: ...
# No account_id check before update_position_tp_sl:528
```

Only `cb_edit_tp_sl_clear:1161` has `if not position or position.account_id != account.id`.

### After (Plan) — Ownership via WHERE (preferred) + state checks

**Search:** `grep -rn "PaperPosition" bot/handlers` and `services/paper_adapter` — every `session.get(PaperPosition` must be replaced or guarded. Also `edit_tp_sl` callbacks carry `position_id` only, no `account_id`.

**Fix for every edit path (5 handlers + 1 text handler):**
1. Resolve `User` → `TradingAccount` first (already does in some, not in `mode`/`only`).
2. Query position with ownership:
```python
pos = (await session.execute(select(PaperPosition).where(PaperPosition.id == pos_id, PaperPosition.account_id == account.id))).scalar_one_or_none()
if not pos: await callback.answer("Позиция не найдена", show_alert=True); return
if pos.status != PositionStatus.OPEN.value: ...
# Competition isolation
competition = await get_active_competition(session)
if pos.competition_id != competition.id: # or use session.get(Competition, pos.competition_id) and check status == ACTIVE and not FINISHED
    await callback.answer("Турнир уже завершён")
# Also check competition tradeable (ends_at > now)
```
3. Verify `trade_state` freshness: `editing_position_id` must equal `pos_id` and `symbol`/`side` must match `pos.symbol`/`pos.side` (prevent stale callback after position closed).

**Handlers to fix (table required by prompt):**

| Path | Handler | Current Auth | Current Ownership | Fix |
|---|---|---|---|---|
| `edit_tp_sl:{id}` | `cb_edit_tp_sl:1028` | `from_user` → `User` → `TradingAccount` yes, but `pos = get(PaperPosition)` no check | **Add** `WHERE account_id == account.id` | Use `select(...).where(id==pos_id, account_id==account.id)` |
| `edit_tp_sl:mode:price:{id}` | `cb_edit_tp_sl_mode:1078` | `from_user` → `pos = get` (no account) | **Add** account lookup + `WHERE` | Same |
| `edit_tp_sl:mode:percent:{id}` | same | — | Add | Same |
| `edit_tp_sl:only:tp:{id}` | `cb_edit_tp_sl_only:1113` | `from_user` → `pos = get` no check | Add | Same |
| `edit_tp_sl:only:sl:{id}` | same | — | Add | Same |
| `edit_tp_sl:only:tp_percent:{id}` | `1113` branch `tp_percent` | — | Add | Same |
| `edit_tp_sl:only:sl_percent:{id}` | same | — | Add | Same |
| `edit_tp_sl:clear:{id}` | `cb_edit_tp_sl_clear:1151` | **Has** `account_id` check `1161` | OK, keep | — |
| `handle_trade_text` edit branch `452` | `pos = get(PaperPosition, pos_id)` `478` | `from_user` → no account check before `update_position_tp_sl:528` | **Add** `WHERE account_id` + `competition` + `OPEN` checks before `update` |
| `update_position_tp_sl` | `services/paper_adapter.py:528` | No `account` param, only `position` + `session` | **Add** `account` param or re-derive and check `position.account_id == account.id` inside + `Competition.status == ACTIVE` + `position.competition_id` matches active | Change signature to `update_position_tp_sl(session, position, account, tp, sl)` and assert ownership |

**Preferred query (ownership):**
```python
# Instead of get, use:
pos = (await session.execute(select(PaperPosition).where(PaperPosition.id == pos_id, PaperPosition.account_id == account.id))).scalar_one_or_none()
```

**State check:** `if pos.status != OPEN: deny`, `if pos.competition_id != active_comp.id: deny` (or check `Competition.status == FINISHED`).

**Transaction:** `update_position_tp_sl` already does `await session.flush()` inside `handle_trade_text`'s `try: ... await session.commit()` — keep, but add `SELECT ... FOR UPDATE` on position row before update (or `with_for_update` in the `select` above).

**Tests (8 required):**
- TEST1 own position → PASS (200, `TP/SL ОБНОВЛЕНЫ`)
- TEST2 userB sends userA `pos_id` via forged `edit_tp_sl:mode:price:123` → DENIED `Позиция не найдена` (no leak whether position exists)
- TEST3 callback manual (craft `callback_data` string) → DENIED
- TEST4 position from another competition (`pos.competition_id != active.id`) → DENIED `Турнир уже завершён`
- TEST5 closed position (`status=CLOSED`) → DENIED
- TEST6 finished competition (`Competition.status=FINISHED`) → DENIED (check `active_comp` is None or `pos.competition_id` not active)
- TEST7 replay old `edit_tp_sl:{old_id}` after position closed → DENIED (status check)
- TEST8 double edit same position concurrent → deterministic (second overwrites or first wins, but both with same `account_id` check, last write wins, no IDOR)

**Additional:** Add `competition_id` to `trade_state` at `cb_edit_tp_sl` time and re-validate at `handle_trade_text` time (stale callback from previous cup).

---

## Cross-Cutting TP/SL Security Table (after fix)

| PATH | HANDLER | AUTH | OWNERSHIP | STATE | COMPETITION | TX |
|---|---|---|---|---|---|---|
| `edit_tp_sl:{id}` | `cb_edit_tp_sl` | `from_user→User→Account` | `WHERE id==pos_id AND account_id==account.id` | `OPEN` | `pos.competition_id==active.id` | `session.flush` inside `handle_trade_text` |
| `edit_tp_sl:mode:*` | `cb_edit_tp_sl_mode` | same | same | `OPEN` | same | same |
| `edit_tp_sl:only:*` | `cb_edit_tp_sl_only` | same | same | `OPEN` | same | same |
| `edit_tp_sl:clear:*` | `cb_edit_tp_sl_clear` | same | **already OK** | `OPEN` | same | same |
| `handle_trade_text` edit branch | `trade.py:452` | same | same (re-check) | `OPEN` | same | `update_position_tp_sl` with `FOR UPDATE` |
| `update_position_tp_sl` | `paper_adapter.py:528` | `account` param | `position.account_id==account.id` assert | `OPEN` | `ACTIVE` | `flush` |

Every mutation path now secure.

---

## Accounting Invariants — Verification Plan

**Helper to add (if not exists):** `services/reconciliation.py:assert_reconciliation(session, account_id)` or inline in tests:

```python
async def assert_reconciliation(session, account_id):
    acc = await session.get(TradingAccount, account_id)
    # 1. No float
    assert all(isinstance(v, Decimal) for v in [acc.cash_balance, acc.margin_used, acc.equity])
    # 2. Ledger vs cash
    ledger_sum = (await session.execute(select(func.sum(AccountLedger.amount)).where(AccountLedger.account_id==account_id))).scalar_one() or Decimal("0")
    # Exclude ADJUSTMENT that is audit-only? For cap case, ADJUSTMENT amount is 0, so sum is cash - initial
    assert (acc.cash_balance - acc.initial_balance).quantize(Decimal("0.01")) == ledger_sum.quantize(Decimal("0.01"))
    # 3. Equity
    unrealized = (await session.execute(select(func.sum(PaperPosition.unrealized_pnl)).where(PaperPosition.account_id==account_id, PaperPosition.status==PositionStatus.OPEN.value))).scalar_one() or Decimal("0")
    assert acc.equity == (acc.cash_balance + acc.margin_used + unrealized).quantize(Decimal("0.01"))
    # 4. No negative balance unless allowed
    assert acc.cash_balance >= 0
    # 5. No duplicate execution
    # ... etc.
```

**Checks after each fix:**
- No `float(` in `services/paper_adapter`, `services/pnl`, `workers/tp_sl_engine`, `bot/handlers/trade` (grep `float\(`)
- Every `account.cash_balance` mutation has `AccountLedger` with same `amount` and `balance_after` in same `begin_nested`
- No `ledger` without financial op, no `financial op` without `ledger` (except `ADJUSTMENT` with `amount=0` + metadata)
- `Execution` exactly one per `PaperPosition` close (unique `position_id` + `execution_reason` + `idempotency` via `paper_orders`)

---

## Database / Migration — Phase 1 Needs

**Does Phase 1 require schema change?**
- Fix #2 (`ADJUSTMENT` with `amount=0`) **does not** require new column/enum — `LedgerType.ADJUSTMENT` already exists (`paper_models.py:26`), `AccountLedger.metadata_json` not needed, we use `reference_type="liquidation_gap"` and `amount=0` with `capped_delta` in `reference_id` or just log. No migration.
- Fix #1, #3, #4 are code-only.
- **Therefore: No new Alembic migration for Phase 1** (unless we decide to add `paper_positions.version` for optimistic locking, which is Phase 2). Verify `upgrade head` from current `009` still works, `downgrade` not needed.

**If we add `LedgerType.LIQUIDATION_GAP` enum value, then migration `010` would be needed (`ALTER TYPE ledger_type ADD VALUE`). For Phase 1 we reuse `ADJUSTMENT` to avoid migration, as `ADJUSTMENT` is generic.

---

## Test Matrix — Phase 1 (minimum new tests)

**Existing suites to run (must stay green):**
1. `pytest -q` (43 passed, 4 PG skipped locally)
2. `pytest tests/test_paper_race_pg.py -v` on `test_railway` (4 passed, now verified)
3. `pytest tests/test_liquidation.py -v` (3 passed)
4. `pytest tests/test_tp_sl_leverage_price.py -v` (10 passed)
5. `pytest tests/test_competition_isolation.py -v` (4 passed)

**New Phase 1 tests (to add):**

*TP/SL parsing (6 tests):*
- `test_single_negative_percent_long` — `"-5"` tp_only + LONG lev10 entry 100 → TP 100.5
- `test_single_negative_percent_short` — `"-5"` tp_only + SHORT
- `test_both_negative_percent` — `"5 -3"` + `"-5 -3"` (copy_abs)
- `test_zero_rejected` — `"0"`, `"0%"`, `"5 0"` → rejected
- `test_excessive_1000_percent` — `1000%` → TP 11*entry lev1, still valid but capped at 500% if we add cap
- `test_comma_decimal` — `"5,5%"` → 5.5%

*IDOR (8 tests):*
- `test_edit_own_pass`, `test_edit_other_denied`, `test_edit_manual_callback`, `test_edit_other_competition`, `test_edit_closed`, `test_edit_finished_comp`, `test_edit_replay_old`, `test_edit_concurrent`

*Accounting (5 tests):*
- `test_normal_loss`, `test_loss_eq_margin`, `test_loss_exceeds_margin_capped`, `test_retry_after_capped`, `test_reconciliation`

*Idempotency (4 tests, PG-only):*
- `test_concurrent_open_same_key`, `test_concurrent_close_same_key`, plus close variants, marked `pytest.mark.pg`

**Total new: ~23 tests. All must be PG-aware (use `pg_engine` fixture, not `sqlite_engine` where `FOR UPDATE` is no-op).**

**Additional checks:**
- `python -m py_compile $(git ls-files '*.py')` — 0 errors
- `grep -rn "float(" --include="*.py" services/ bot/ workers/` — 0 in money paths
- `grep -rn "session.get(PaperPosition" bot/handlers/` — 0 after fix (all replaced with `where account_id`)
- `grep -rn "ADJUSTMENT" services/` — 1 new in `paper_adapter`

---

## Security Re-Audit (post-fix, expected)

| Original Finding | After Fix | Status |
|---|---|---|
| H1 single `-5` rejected | `val ==0` check + `copy_abs` | **FIXED** |
| H2 `0%` inconsistency | Both-percent `tp_pct==0` reject | **FIXED** |
| H3 comma destroyed | `replace(",", ".")` before split | **FIXED** |
| F4 loss capping creates money | `ADJUSTMENT` 0 + metadata + cap | **FIXED** (auditable) |
| C1 idempotency commit visibility | Single savepoint for order+position+ledger+account | **FIXED** |
| IDOR `edit_tp_sl:mode/only` | `WHERE id==pos_id AND account_id==account.id` | **FIXED** |
| Banned can close/edit | `ensure_can_trade` added to all 5 handlers + `update_position_tp_sl` | **FIXED** |

**Remaining (Phase 2):** `M1` quantization tick, `M2` is_percent ambiguity (documented), `M3` lev0 DivisionByZero, `F1` double quantization drift, `F10` triple equity source, `C3` lost idempotency on ledger already fixed, `J` duplicate snapshot/prize (Phase 2).

---

## Regression Check (must hold)

- `LONG OPEN = ASK` `paper_adapter:176` vs `LONG CLOSE = BID` `paper_adapter:399` — unchanged
- `SHORT OPEN = BID` vs `SHORT CLOSE = ASK` — unchanged
- `LONG TP 20% lev10 entry 50000 → 51000` — unchanged (profit-based, not price*2)
- `SHORT TP 20% lev10 entry 50000 → 49000` — unchanged
- `LONG SL 20% lev10 → 49000` — unchanged
- `100% still means PnL == margin` — unchanged (entry*(1+1/lev))
- Decimal everywhere — unchanged
- `test_demo_acceptance` LONG ASK 50010 / SHORT BID 50000 — must still pass

---

## Final Status (after Phase 1 plan, before implementation)

**AUDIT ONLY — NOT COMMITTED — NOT DEPLOYED — NO FILES MODIFIED**

**Next step:** Await explicit `IMPLEMENT PHASE 1` approval. Upon approval, implement in order Fix #1 → #4 → #2 → #3, each with migration check (only Fix #2 may need `ADJUSTMENT` metadata, no new enum), then run full matrix (existing 43 + new 23), then security re-audit via `grep` + `pytest -k pg` on `test_railway`, then diff review and re-audit Phase 1 before report.

**Blocker if not approved:** System remains with **HIGH IDOR** (edit any position) and **CRITICAL financial** (retry after commit shows spurious error, not money loss yet due to cap, but double-ledger risk remains).

