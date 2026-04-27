# HighTrade Postmortem Follow-Up: Quantitative Analysis
**Date**: 2026-04-10  
**Scope**: Verify postmortem claims against trading_history.db; document DB findings; catalogue fixes implemented.

---

## 1. Database Verification

### P&L Summary (closed trades only, excluding archived)

| Symbol | Trades | Total P&L | Avg P&L | Wins | Exit reasons |
|--------|--------|-----------|---------|------|--------------|
| BRZE   | 3      | +$51.22   | +$17.07 | 3/3  | manual, manual, manual |
| TTD    | 10     | +$24.40   | +$2.44  | 4/10 | 9× manual, 1× stop_loss |
| SGML   | 3      | -$23.76   | -$7.92  | 0/3  | manual, manual, stop_loss |
| GSAT   | 2      | -$23.04   | -$11.52 | 0/2  | manual, stop_loss |
| BTC-USD| 3      | -$22.33   | -$7.44  | 0/3  | profit_target, manual, stop_loss |
| HUM    | 1      | -$20.72   | -$20.72 | 0/1  | invalidation |
| NCNO   | 2      | -$6.46    | -$3.23  | 0/2  | manual, stop_loss |
| APLS   | 3      | -$3.08    | -$1.03  | 0/3  | manual, invalidation, manual |
| **Total** | **32** | **-$23.77** | | **7/32** | |

### Win rate: 7/32 = **21.9%**

---

## 2. DAL Correction: Postmortem Figure Was Inflated

The postmortem stated **DAL = -$419.85** across 9 trades.

Reality: **all 9 DAL records are `status='archived'`**, excluded from P&L accounting.  
The records were created by a Alpaca sync bug that duplicated the same position 7×.  
- `trade_id=41`: one real daytrade exit, `-$43.50` (manual)  
- `trade_id=49`: one held-overnight exit, `-$57.85` (manual)  
- `trade_ids=42–48`: **7 Alpaca sync duplicates** with the same values (`-$45.50` each), all archived 2026-04-09.

**Real DAL loss: ≈ -$101.35** (trade_ids 41 + 49).

This does not change the strategic lesson — DAL was the worst single idea —  
but the scale was 4× overstated in the original postmortem.

**Root cause of duplicates**: `_sync_open_positions_from_alpaca()` in `paper_trading.py`  
imported the same Alpaca position on multiple sync cycles without deduplication.  
The cleanup on 2026-04-09 archived the duplicates but did not consolidate them.

---

## 3. Entry Thesis Metadata Gap

**Observation**: `entry_signal_score` = `0` for **all 32 closed trades** in the DB.  
The field existed but was never written with real data.  

Other thesis fields (`entry_thesis_text`, `entry_catalyst_text`, etc.) did not exist yet.  
→ **Fix implemented**: see §5 below.

---

## 4. Portfolio Snapshots Gap

**Observation**: Only **1 portfolio snapshot** on record (2026-03-17, $999.27).  
No code in the codebase was writing to `portfolio_snapshots` after initial setup.  
→ **Fix implemented**: see §5 below.

---

## 5. Fixes Implemented (2026-04-10)

### Fix 1: Entry thesis preservation — `trade_thesis.py` (new file)

New module at `/Users/traderbot/Documents/highTRADE/trade_thesis.py`.

**Schema additions to `trade_records`** (DDL runs on import, idempotent):
- `entry_thesis_text TEXT` — reasoning chain at time of buy
- `entry_catalyst_text TEXT` — catalyst description at entry  
- `entry_signal_breakdown TEXT` — JSON per-signal score breakdown
- `entry_regime_context TEXT` — DEFCON / market regime context
- `entry_discovery_score REAL` — discovery score 0–100
- `entry_catalyst_score REAL` — catalyst score 0–100
- `entry_regime_score REAL` — regime score 0–100
- `entry_conviction REAL` — overall conviction 0–100

**New table**: `thesis_invalidation_log`  
Tracks every stop-loss / invalidation exit with cooldown metadata.

**API**:
```python
save_entry_thesis(conn, trade_id, thesis_text=..., catalyst_text=..., signal_score=...)
record_thesis_invalidation(conn, ticker, trade_id, exit_reason, exit_price, ...)
allowed, reason = check_reentry_allowed(conn, ticker, new_catalyst=...)
get_invalidation_summary(conn, ticker, days=30)
```

### Fix 2: Anti-reentry gating

**Cooldown rules**:
| Exit type     | Cooldown | Override |
|---------------|----------|---------|
| `stop_loss`   | 24 h     | Yes — if `new_catalyst` text differs by >40% word overlap |
| `invalidation`| 48 h     | Yes — same logic |
| 2+ stop-outs in 7 days | 24 h | **No** — hard block, manual review required |

**Where enforced**:
- `manual_buy()` in `paper_trading.py`: warns in the return message (human override always allowed)
- `process_acquisition_conditionals()` in `broker_agent.py`: **hard blocks** automated entries
  and logs `result="BLOCKED_REENTRY"` to the decision log

**Hooks to add in future** (out of scope for this session — require careful testing):
- Day trader scan: call `check_reentry_allowed` before adding to candidate list
- Acquisition researcher: pass thesis summary to `save_entry_thesis` after entry

### Fix 3: Portfolio snapshots — `hightrade_orchestrator.py`

Added a lightweight snapshot writer to the monitoring cycle.  
Fires every **2 monitoring cycles** (~30 min at default 15-min cycle interval).

Writes one row to `portfolio_snapshots` per fire:
- `total_value`, `cash_balance`, `deployed_capital`
- `unrealized_pnl`, `realized_pnl`, `total_return_pct`
- `open_positions_json` (compact: symbol, shares, entry_price, current_price)

Uses real Alpaca equity when available; falls back to DB-computed values.  
Errors are caught silently — never breaks the monitoring cycle.

---

## 6. Files Changed

| File | Change |
|------|--------|
| `trade_thesis.py` | **NEW**: thesis metadata, anti-reentry gating, invalidation log |
| `paper_trading.py` | Added `trade_thesis` import + `save_entry_thesis` call in `manual_buy()` + `record_thesis_invalidation` call in `exit_position()` + anti-reentry warning gate in `manual_buy()` |
| `broker_agent.py` | Added anti-reentry hard gate in `process_acquisition_conditionals()` + `save_entry_thesis` call after successful conditional entry |
| `hightrade_orchestrator.py` | Added portfolio snapshot writer to monitoring cycle |
| `trading_data/trading_history.db` | Schema migration run: 8 new columns in `trade_records` + `thesis_invalidation_log` table created |

---

## 7. What Still Needs Work (Not Implemented — Needs More Care)

1. **Populate `entry_signal_score` from actual analysis calls**  
   The acquisition analyst and verifier should pass their confidence/score into `save_entry_thesis()`.  
   Currently the score is 0 for all trades — the function is now wired but data sources need to pass scores.

2. **Day trader reentry check**  
   `day_trader.py` scans independently. `check_reentry_allowed()` should be called in  
   `_get_candidates()` or equivalent before adding to the scan list.

3. **DAL duplicate cleanup**  
   The 7 archived duplicates could be purged from the DB if desired, but they are `archived`  
   (not `closed`) so they don't affect P&L calculations. Leave for manual decision.

4. **Regime scoring integration**  
   The postmortem called for stronger `regime_score` weighting. This requires changes to  
   the acquisition analyst prompt, which is a live-money-adjacent path — left for a dedicated session.

5. **Backfill historical thesis data**  
   Existing closed trades have no thesis metadata. A one-time backfill from `catalyst_event`  
   and `notes` would improve postmortem quality.

---

## 8. Edge Stack Scorecard (proposed, not yet implemented in scoring)

Per the postmortem's "practical next steps":

| Component | Where it comes from | Status |
|-----------|---------------------|--------|
| Discovery score | grok_hound_candidates.model_confidence | ❌ not stored in trade_records |
| Catalyst score | conditional_tracking.research_confidence | ❌ not stored in trade_records |
| Regime score | defcon_level (coarse proxy) | ✅ defcon_at_entry exists |
| Execution score | exit vs. entry price delta | ⚠️ derivable post-hoc |
| Conviction threshold | research_confidence ≥ 0.7 gate | ❌ not enforced at entry |

Storing these five numbers per trade is now unblocked by the new schema.  
The analyst and researcher pipelines need to pass values through `save_entry_thesis()`.

---

*Generated by subagent postmortem analysis — 2026-04-10*
