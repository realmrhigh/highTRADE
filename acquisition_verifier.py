#!/usr/bin/env python3
"""
acquisition_verifier.py — Daily Flash re-verification of active and low-priority conditionals.

Runs once per day (called from orchestrator verifier cycle).
For each 'active' conditional in conditional_tracking it feeds a compact
snapshot of current price, recent news, and macro to Gemini Flash (fast tier,
no thinking budget) and asks: confirm / flag / invalidate.

Degradation ladder
──────────────────
  active
    │  verdict = confirm             → stays active (last_verified refreshed)
    │  verdict = flag (< 5 times)   → stays active, flag_count++, note added
    │  verdict = flag (≥ 5 times)   → demoted to low_priority
    │  verdict = invalidate          → see confidence routing below
    ▼
  low_priority
    │  (checked at most once per 3 days — re-engages quickly on market recovery)
    │  verdict = confirm             → restored to active, flag_count reset to 0
    │  verdict = flag / invalidate   → see confidence routing
    ▼
  archived (terminal)

Confidence routing on invalidate
──────────────────────────────────
  corrected_confidence ≥ 0.60   → restored to active with corrected entry/stop/TP/conditions
  corrected_confidence 0.25–0.59 → low_priority with corrected levels (weekly cadence)
  corrected_confidence < 0.25   → invalidated (terminal)
  terminal = true               → invalidated regardless of confidence

After invalidation_count ≥ 2 AND corrected_confidence < 0.25 → archived outright.
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import gemini_client

logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent.resolve()
DB_PATH    = SCRIPT_DIR / 'trading_data' / 'trading_history.db'

# Thresholds
ACTIVE_CONFIDENCE_THRESHOLD  = 0.60   # corrected_conf >= this → restored to active
LOW_PRIORITY_THRESHOLD       = 0.25   # corrected_conf >= this → low_priority
FLAG_DEMOTION_THRESHOLD      = 5      # consecutive flags before demotion to low_priority
                                      # (5 = ~5 hours of bad macro before parking a lead)
LOW_PRIORITY_COOLDOWN_DAYS   = 3      # low_priority items re-checked every 3 days
                                      # (re-engages fast when market recovers)
MAX_INVALIDATIONS_BEFORE_ARCHIVE = 2  # archive after this many invalidations with conf < 0.25

# Per-cycle caps — prevent a single verifier run from consuming the entire Flash RPM budget.
# At 120 RPM with 0.5s spacing, 10 actives = ~5s + 3 LP = ~1.5s = ~6.5s per cycle.
# Remaining RPM headroom is preserved for analyst, broker, briefing calls.
MAX_ACTIVE_PER_CYCLE = 10   # top-N active conditionals processed per orchestrator cycle
MAX_LP_PER_CYCLE     = 3    # low-priority conditionals checked per orchestrator cycle

_VERIFIER_JSON_TEMPLATE = """{
  "verdict": "confirm",
  "confidence_adjustment": 0.0,
  "flag_reason": "",
  "invalidation_reason": "",
  "corrected_entry_target": null,
  "corrected_stop_loss": null,
  "corrected_take_profit_1": null,
  "corrected_take_profit_2": null,
  "corrected_entry_conditions": [],
  "corrected_confidence": 0.0,
  "correction_rationale": "",
  "terminal": false,
  "terminal_reason": "",
  "updated_thesis": "",
  "price_still_valid": true,
  "reasoning": "brief explanation",
  "data_gaps": ["<data absent today that would have made this verification sharper — e.g. 'no recent earnings transcript', 'short interest data stale', 'options flow unavailable'>"]
}"""


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _fetch_recent_news_for_ticker(ticker: str, conn: sqlite3.Connection) -> List[str]:
    """Pull the 3 most recent news signals mentioning this ticker."""
    since = (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d')
    try:
        cursor = conn.execute("""
            SELECT timestamp, news_score, sentiment_summary, dominant_crisis_type
            FROM news_signals
            WHERE DATE(timestamp) >= ? AND keyword_hits_json LIKE ?
            ORDER BY news_score DESC LIMIT 3
        """, (since, f'%{ticker}%'))
        rows = cursor.fetchall()
        return [
            f"[{r['timestamp'][:16]}] score={r['news_score']} {r['dominant_crisis_type']}: {r['sentiment_summary']}"
            for r in rows
        ]
    except Exception:
        return []


def _get_current_price(ticker: str) -> Optional[float]:
    """Fetch current price via yfinance."""
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period='1d')
        return float(hist['Close'].iloc[-1]) if len(hist) > 0 else None
    except Exception:
        return None


def _get_latest_macro(conn: sqlite3.Connection) -> Dict:
    """Get latest macro snapshot."""
    try:
        cursor = conn.execute("""
            SELECT macro_score, yield_curve_spread, hy_oas_bps, consumer_sentiment
            FROM macro_indicators ORDER BY created_at DESC LIMIT 1
        """)
        row = cursor.fetchone()
        return dict(row) if row else {}
    except Exception:
        return {}


def _build_verifier_prompt(cond: Dict, current_price: Optional[float],
                            recent_news: List[str], macro: Dict,
                            is_low_priority: bool = False) -> str:
    """Build the compact Flash verification prompt.

    When verdict is 'invalidate', the model MUST also provide corrected conditionals
    so the system can decide whether to restore, demote, or kill the position.
    """
    ticker      = cond['ticker']
    entry_tgt   = cond.get('entry_price_target', 'N/A')
    stop        = cond.get('stop_loss', 'N/A')
    tp1         = cond.get('take_profit_1', 'N/A')
    thesis      = cond.get('thesis_summary', 'N/A')
    confidence  = cond.get('research_confidence', 0)
    date_set    = cond.get('date_created', 'N/A')
    watch_tag   = (cond.get('watch_tag') or 'untagged').lower()
    conditions  = json.loads(cond.get('entry_conditions_json') or '[]')
    invalidates = json.loads(cond.get('invalidation_conditions_json') or '[]')
    inval_count = cond.get('invalidation_count') or 0
    flag_count  = cond.get('flag_count') or 0
    priority    = cond.get('priority') or 'normal'

    price_str   = f"${current_price:.2f}" if current_price else 'N/A'
    distance    = None
    if current_price and isinstance(entry_tgt, (int, float)):
        distance = (current_price - entry_tgt) / entry_tgt * 100

    # Directional context: breakout entries WANT price above target
    is_breakout = watch_tag in ('breakout',)
    if is_breakout:
        if distance is not None and distance > 0:
            distance_str = f"{distance:+.1f}% ABOVE target (breakout confirmed — this is BULLISH)"
        elif distance is not None:
            distance_str = f"{distance:+.1f}% below breakout target (not yet triggered)"
        else:
            distance_str = ''
    else:
        distance_str = f"{distance:+.1f}% from entry target" if distance is not None else ''

    news_text = '\n'.join(f"  • {n}" for n in recent_news) if recent_news else '  • No recent mentions'
    cond_text = '\n'.join(f"  • {c}" for c in conditions[:3]) if conditions else '  • N/A'
    inv_text  = '\n'.join(f"  • {c}" for c in invalidates[:2]) if invalidates else '  • N/A'

    macro_text = ''
    if macro:
        ms = macro.get('macro_score', 'N/A')
        yc = macro.get('yield_curve_spread', 'N/A')
        yc_str = f"{yc:+.2f}%" if isinstance(yc, float) else str(yc)
        macro_text = f"  Macro score: {ms}  |  Yield curve: {yc_str}\n"

    status_note = ''
    if is_low_priority:
        status_note = (
            f"\n⚠️  NOTE: This conditional is already LOW-PRIORITY "
            f"(prior invalidations={inval_count}, flags={flag_count}). "
            f"It needs a compelling case to stay in the system.\n"
        )
    elif inval_count > 0 or flag_count > 0:
        status_note = (
            f"\n📋 History: {inval_count} prior invalidation(s), {flag_count} prior flag(s).\n"
        )

    # Breakout tag context for the verifier model
    tag_context = ''
    if is_breakout:
        tag_context = (
            f"\n⚡ ENTRY TYPE: BREAKOUT — price ABOVE target means the breakout is CONFIRMED.\n"
            f"  Do NOT invalidate because price is above target. For breakout entries,\n"
            f"  the entry target is a SUPPORT FLOOR, not a ceiling. Price above target = bullish.\n"
            f"  Only invalidate if the thesis itself has failed (e.g., catalyst reversed).\n"
        )
    elif watch_tag != 'untagged':
        tag_context = f"\n  Entry type: {watch_tag}\n"

    # Pull time horizon from conditional
    time_horizon = cond.get('time_horizon_days') or 5

    # Strategy misalignment check
    _stale_flag = ''
    try:
        created_dt = datetime.strptime(cond.get('date_created', ''), '%Y-%m-%d')
        days_old = (datetime.now() - created_dt).days
        if days_old >= time_horizon:
            _stale_flag = (
                f"\n⏰ STALE ALERT: This conditional was set {days_old} day(s) ago "
                f"with a {time_horizon}-day time horizon. It has EXPIRED its window.\n"
                f"   Unless the thesis has a new catalyst, verdict should be 'invalidate'.\n"
            )
    except Exception:
        days_old = 0

    return (
        f"You are a trading system verifier. Today is {datetime.now().strftime('%Y-%m-%d')}.\n"
        f"A Gemini 3 Pro analyst set a conditional entry on {ticker} on {date_set}.\n"
        f"Your job: quickly decide if this conditional is still VALID.\n\n"
        f"STRATEGY ALIGNMENT CHECK — verify against this before anything else:\n"
        f"  This is a FLIP-AND-BANK system. Max hold = 5 days. We do NOT hold recovery plays.\n"
        f"  Flag or invalidate if ANY of these are true:\n"
        f"  • The thesis relies on 'waiting for macro to stabilize / market to recover'\n"
        f"  • The time horizon is > 5 days or the conditional is already past its window\n"
        f"  • The setup is a mega-cap mean-reversion (NVDA/MSFT/GOOGL/AAPL) with no specific 48h catalyst\n"
        f"  • The price hasn't moved toward the entry target in {min(time_horizon, 3)} or more days\n\n"
        f"{status_note}{tag_context}{_stale_flag}\n"
        f"CONDITIONAL SUMMARY\n"
        f"  Thesis: {thesis}\n"
        f"  Entry target: ${entry_tgt}  |  Stop: ${stop}  |  TP1: ${tp1}\n"
        f"  Watch tag: {watch_tag}  |  Time horizon: {time_horizon} days  |  Original confidence: {confidence:.2f}\n\n"
        f"ENTRY CONDITIONS\n{cond_text}\n\n"
        f"INVALIDATION TRIGGERS\n{inv_text}\n\n"
        f"CURRENT STATE ({datetime.now().strftime('%Y-%m-%d')})\n"
        f"  Current price: {price_str} {distance_str}\n"
        f"{macro_text}\n"
        f"RECENT NEWS MENTIONS\n{news_text}\n\n"
        f"VERDICT OPTIONS:\n"
        f"  confirm    — thesis intact, conditional still valid, catalyst is imminent\n"
        f"  flag       — concern raised, analyst should review, but don't kill it yet\n"
        f"  invalidate — a core invalidation condition has triggered OR thesis has clearly failed\n\n"
        f"CRITICAL: If your verdict is 'invalidate', you MUST provide corrected conditionals.\n"
        f"Ask yourself: at what price/conditions could this thesis still work?\n"
        f"  • Set corrected_confidence based on how convinced you still are (0.0–1.0)\n"
        f"  • If the company is failing, delisted, or thesis is completely dead: "
        f"set terminal=true and corrected_confidence < 0.25\n"
        f"  • If the setup just needs recalibration: provide corrected entry/stop/TP/conditions\n"
        f"  • corrected_confidence < 0.25 → system will archive this ticker\n"
        f"  • corrected_confidence 0.25–0.59 → system will demote to low-priority watch list\n"
        f"  • corrected_confidence ≥ 0.60 → system will restore to active with new levels\n\n"
        f"Respond ONLY in this exact JSON format (no other text):\n"
        f"{_VERIFIER_JSON_TEMPLATE}"
    )


def _parse_verifier_response(text: str) -> Dict:
    """Parse Flash JSON response with code-fence stripping."""
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        return {'verdict': 'confirm', 'reasoning': 'parse_failed', '_parse_failed': True}


def _merge_and_store_data_gaps(conn: sqlite3.Connection, cond_id: int,
                                new_gaps: List[str], existing_json: Optional[str]) -> None:
    """Merge verifier data_gaps with existing analyst gaps and write back to conditional_tracking."""
    if not new_gaps:
        return
    try:
        existing: List[str] = json.loads(existing_json) if existing_json else []
        # Deduplicate by lowercased text (keep insertion order, new gaps last)
        seen = {g.lower().strip() for g in existing}
        merged = list(existing)
        for g in new_gaps:
            if g.lower().strip() not in seen:
                merged.append(g)
                seen.add(g.lower().strip())
        # Keep at most 20 gaps
        merged = merged[-20:]
        conn.execute(
            "UPDATE conditional_tracking SET data_gaps_json=?, updated_at=? WHERE id=?",
            (json.dumps(merged), datetime.now().isoformat(), cond_id)
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"  ⚠️  Failed to merge data_gaps for cond_id={cond_id}: {e}")


def _write_corrected_levels(conn: sqlite3.Connection, cond_id: int, result: Dict,
                             new_status: str, now_iso: str, new_conf: float,
                             new_inval_count: int, new_flag_count: int,
                             new_priority: str, notes: str):
    """Apply corrected conditionals from LLM back to conditional_tracking."""
    corrected_entry = result.get('corrected_entry_target')
    corrected_stop  = result.get('corrected_stop_loss')
    corrected_tp1   = result.get('corrected_take_profit_1')
    corrected_tp2   = result.get('corrected_take_profit_2')
    corrected_conds = result.get('corrected_entry_conditions') or []
    updated_thesis  = result.get('updated_thesis') or ''
    correction_rat  = result.get('correction_rationale') or ''

    conn.execute("""
        UPDATE conditional_tracking
        SET status              = ?,
            research_confidence = ?,
            entry_price_target  = COALESCE(?, entry_price_target),
            stop_loss           = COALESCE(?, stop_loss),
            take_profit_1       = COALESCE(?, take_profit_1),
            take_profit_2       = COALESCE(?, take_profit_2),
            entry_conditions_json = CASE
                WHEN ? != '[]' THEN ?
                ELSE entry_conditions_json
            END,
            thesis_summary      = CASE WHEN ? != '' THEN ? ELSE thesis_summary END,
            verification_notes  = ?,
            last_verified       = ?,
            invalidation_count  = ?,
            flag_count          = ?,
            priority            = ?,
            updated_at          = ?
        WHERE id = ?
    """, (
        new_status,
        new_conf,
        corrected_entry, corrected_stop, corrected_tp1, corrected_tp2,
        json.dumps(corrected_conds), json.dumps(corrected_conds),
        updated_thesis, updated_thesis,
        notes,
        now_iso,
        new_inval_count,
        new_flag_count,
        new_priority,
        now_iso,
        cond_id,
    ))


def run_verification_cycle() -> Dict:
    """
    Main entry point — called by orchestrator verifier cycle.

    Iterates all 'active' conditionals (every run) and 'low_priority' conditionals
    (only if last_verified > 7 days ago) and runs Flash verification on each.

    Status transitions:
      confirm           → stays active / low_priority promoted to active if conf ≥ 0.60
      flag (< 3 times)  → stays active, flag_count++
      flag (≥ 3 times)  → demoted to low_priority
      invalidate:
        conf ≥ 0.60     → restored to active with corrected levels
        conf 0.25–0.59  → low_priority with corrected levels
        conf < 0.25 / terminal → invalidated (terminal), then archived next pass

    Returns summary dict: {confirmed, flagged, invalidated, corrected, demoted, archived, errors}
    """
    date_str = datetime.now().strftime('%Y-%m-%d')
    logger.info(f"🔍 Acquisition Verifier: starting cycle for {date_str}")

    conn = _get_conn()
    summary = {
        'confirmed':   0,
        'flagged':     0,
        'invalidated': 0,
        'corrected':   0,   # restored to active with new levels
        'demoted':     0,   # moved to low_priority
        'archived':    0,   # terminal — moved to invalidated (final)
        'errors':      0,
    }

    try:
        # Active conditionals — capped to MAX_ACTIVE_PER_CYCLE per run, sorted by priority
        active_rows = conn.execute("""
            SELECT id, ticker, date_created, entry_price_target, stop_loss,
                   take_profit_1, take_profit_2, thesis_summary, research_confidence,
                   entry_conditions_json, invalidation_conditions_json,
                   verification_count, time_horizon_days,
                   invalidation_count, flag_count, priority, last_verified,
                   data_gaps_json, watch_tag
            FROM conditional_tracking
            WHERE status = 'active'
            ORDER BY COALESCE(attention_score, 0) DESC, research_confidence DESC
            LIMIT ?
        """, (MAX_ACTIVE_PER_CYCLE,)).fetchall()

        # Low-priority — only if not verified in the last 3 days, capped to MAX_LP_PER_CYCLE
        lp_cutoff = (datetime.now() - timedelta(days=LOW_PRIORITY_COOLDOWN_DAYS)).isoformat()
        lp_rows = conn.execute("""
            SELECT id, ticker, date_created, entry_price_target, stop_loss,
                   take_profit_1, take_profit_2, thesis_summary, research_confidence,
                   entry_conditions_json, invalidation_conditions_json,
                   verification_count, time_horizon_days,
                   invalidation_count, flag_count, priority, last_verified,
                   data_gaps_json, watch_tag
            FROM conditional_tracking
            WHERE status = 'low_priority'
              AND (last_verified IS NULL OR last_verified < ?)
            ORDER BY research_confidence DESC
            LIMIT ?
        """, (lp_cutoff, MAX_LP_PER_CYCLE)).fetchall()

        actives = [dict(r) for r in active_rows]
        lp_list = [dict(r) for r in lp_rows]

    except Exception as e:
        logger.error(f"Failed to fetch conditionals: {e}")
        conn.close()
        return summary

    all_to_verify = [(c, False) for c in actives] + [(c, True) for c in lp_list]

    if not all_to_verify:
        logger.info("  📭 No conditionals to verify this cycle")
        conn.close()
        return summary

    active_tickers = [c['ticker'] for c in actives]
    lp_tickers     = [c['ticker'] for c in lp_list]
    logger.info(
        f"  📋 {len(actives)} active: {active_tickers} | "
        f"{len(lp_list)} low-priority due: {lp_tickers}"
    )

    macro = _get_latest_macro(conn)

    for cond, is_low_priority in all_to_verify:
        ticker  = cond['ticker']
        cond_id = cond['id']

        lp_tag = ' [LOW-PRI]' if is_low_priority else ''
        logger.info(f"  🔎 Verifying {ticker}{lp_tag}...")

        try:
            current_price = _get_current_price(ticker)
            recent_news   = _fetch_recent_news_for_ticker(ticker, conn)
            prompt        = _build_verifier_prompt(cond, current_price, recent_news, macro,
                                                   is_low_priority=is_low_priority)

            text, in_tok, out_tok = gemini_client.call(
                prompt=prompt,
                model_key='fast',   # No thinking — cheap and fast
                caller='verifier',
            )

            if not text:
                logger.warning(f"  ⚠️  {ticker}: empty response from Flash")
                summary['errors'] += 1
                continue

            result  = _parse_verifier_response(text)
            verdict = result.get('verdict', 'confirm').lower().strip()

            logger.info(
                f"  📊 {ticker}: verdict={verdict} "
                f"({in_tok}→{out_tok} tok) | {result.get('reasoning','')[:80]}"
            )

            now_iso       = datetime.now().isoformat()
            new_ver_count = (cond.get('verification_count') or 0) + 1
            inval_count   = cond.get('invalidation_count') or 0
            flag_count    = cond.get('flag_count') or 0
            priority      = cond.get('priority') or 'normal'

            # ── CONFIRM ─────────────────────────────────────────────────────
            if verdict == 'confirm':
                if is_low_priority:
                    # Low-priority with a confirmed thesis → promote back to active
                    new_conf = min(
                        float(cond.get('research_confidence') or 0) +
                        float(result.get('confidence_adjustment') or 0),
                        1.0
                    )
                    conn.execute("""
                        UPDATE conditional_tracking
                        SET status='active', last_verified=?, verification_count=?,
                            flag_count=0, priority='normal',
                            research_confidence=?, updated_at=?
                        WHERE id=?
                    """, (now_iso, new_ver_count, max(new_conf, 0.0), now_iso, cond_id))
                    logger.info(f"  ⬆️  {ticker} LOW-PRI PROMOTED back to active (confirmed)")
                    summary['confirmed'] += 1
                else:
                    conn.execute("""
                        UPDATE conditional_tracking
                        SET last_verified=?, verification_count=?, updated_at=?
                        WHERE id=?
                    """, (now_iso, new_ver_count, now_iso, cond_id))
                    logger.info(f"  ✅ {ticker} confirmed valid")
                    summary['confirmed'] += 1

            # ── FLAG ─────────────────────────────────────────────────────────
            elif verdict == 'flag':
                new_flag_count = flag_count + 1
                flag_note = (
                    f"[FLAGGED {date_str} #{new_flag_count}] "
                    f"{result.get('flag_reason', result.get('reasoning', ''))}"
                )

                if new_flag_count >= FLAG_DEMOTION_THRESHOLD:
                    # Too many consecutive flags → demote to low_priority
                    conn.execute("""
                        UPDATE conditional_tracking
                        SET status='low_priority', priority='low',
                            verification_notes=?, last_verified=?,
                            verification_count=?, flag_count=?, updated_at=?
                        WHERE id=?
                    """, (flag_note, now_iso, new_ver_count, new_flag_count, now_iso, cond_id))
                    logger.warning(
                        f"  ⬇️  {ticker} DEMOTED to low_priority "
                        f"({new_flag_count} consecutive flags)"
                    )
                    summary['demoted'] += 1
                else:
                    conn.execute("""
                        UPDATE conditional_tracking
                        SET verification_notes=?, last_verified=?,
                            verification_count=?, flag_count=?, updated_at=?
                        WHERE id=?
                    """, (flag_note, now_iso, new_ver_count, new_flag_count, now_iso, cond_id))
                    logger.warning(
                        f"  🚩 {ticker} FLAGGED ({new_flag_count}/{FLAG_DEMOTION_THRESHOLD}): "
                        f"{result.get('flag_reason','')}"
                    )
                    summary['flagged'] += 1

            # ── INVALIDATE ───────────────────────────────────────────────────
            elif verdict == 'invalidate':
                new_inval_count   = inval_count + 1
                corrected_conf    = float(result.get('corrected_confidence') or 0.0)
                is_terminal       = bool(result.get('terminal', False))
                terminal_reason   = result.get('terminal_reason', '')
                inval_reason      = result.get('invalidation_reason', result.get('reasoning', ''))
                correction_rat    = result.get('correction_rationale', '')

                # Terminal or confidence too low → archive / hard kill
                if is_terminal or corrected_conf < LOW_PRIORITY_THRESHOLD:
                    if new_inval_count >= MAX_INVALIDATIONS_BEFORE_ARCHIVE or is_terminal:
                        # Fully archive
                        arch_note = (
                            f"[ARCHIVED {date_str}] Terminal={is_terminal}. "
                            f"{terminal_reason or inval_reason} | "
                            f"corrected_conf={corrected_conf:.2f}"
                        )
                        conn.execute("""
                            UPDATE conditional_tracking
                            SET status='invalidated', priority='low',
                                verification_notes=?, last_verified=?,
                                verification_count=?, invalidation_count=?,
                                research_confidence=?, updated_at=?
                            WHERE id=?
                        """, (
                            arch_note, now_iso, new_ver_count,
                            new_inval_count, corrected_conf, now_iso, cond_id
                        ))
                        # Also mark acquisition_watchlist
                        conn.execute("""
                            UPDATE acquisition_watchlist SET status='invalidated'
                            WHERE UPPER(ticker) = UPPER(?)
                              AND status IN ('conditional_set','researched','analyst_pass')
                        """, (ticker,))
                        logger.info(
                            f"  💀 {ticker} TERMINAL INVALIDATED "
                            f"(conf={corrected_conf:.2f}, inval#{new_inval_count})"
                        )
                        summary['archived'] += 1
                    else:
                        # First terminal-level invalidation — move to low_priority and wait
                        notes = (
                            f"[INVALIDATED→LOW-PRI {date_str} #{new_inval_count}] "
                            f"{inval_reason} | corrected_conf={corrected_conf:.2f}"
                        )
                        conn.execute("""
                            UPDATE conditional_tracking
                            SET status='low_priority', priority='low',
                                verification_notes=?, last_verified=?,
                                verification_count=?, invalidation_count=?,
                                research_confidence=?, updated_at=?
                            WHERE id=?
                        """, (
                            notes, now_iso, new_ver_count,
                            new_inval_count, corrected_conf, now_iso, cond_id
                        ))
                        logger.warning(
                            f"  ⬇️  {ticker} INVALIDATED→LOW-PRI "
                            f"(conf={corrected_conf:.2f}, first invalidation)"
                        )
                        summary['demoted'] += 1

                # Strong correction — restore to active with corrected levels
                elif corrected_conf >= ACTIVE_CONFIDENCE_THRESHOLD:
                    notes = (
                        f"[CORRECTED {date_str}] {inval_reason} → "
                        f"Restored at conf={corrected_conf:.2f}. {correction_rat}"
                    )
                    _write_corrected_levels(
                        conn, cond_id, result,
                        new_status='active',
                        now_iso=now_iso,
                        new_conf=corrected_conf,
                        new_inval_count=new_inval_count,
                        new_flag_count=0,         # reset flags on successful correction
                        new_priority='normal',
                        notes=notes,
                    )
                    logger.info(
                        f"  🔄 {ticker} CORRECTED & RESTORED active "
                        f"(conf={corrected_conf:.2f}) | {correction_rat[:60]}"
                    )
                    summary['corrected'] += 1

                # Moderate correction — demote to low_priority with corrected levels
                else:  # 0.25 <= corrected_conf < 0.60
                    notes = (
                        f"[INVALIDATED→LOW-PRI {date_str} #{new_inval_count}] "
                        f"{inval_reason} → corrected_conf={corrected_conf:.2f}. {correction_rat}"
                    )
                    _write_corrected_levels(
                        conn, cond_id, result,
                        new_status='low_priority',
                        now_iso=now_iso,
                        new_conf=corrected_conf,
                        new_inval_count=new_inval_count,
                        new_flag_count=flag_count,
                        new_priority='low',
                        notes=notes,
                    )
                    logger.warning(
                        f"  ⬇️  {ticker} INVALIDATED→LOW-PRI "
                        f"(conf={corrected_conf:.2f}) | {correction_rat[:60]}"
                    )
                    summary['demoted'] += 1

            else:
                # Unknown verdict — treat as confirm
                conn.execute("""
                    UPDATE conditional_tracking
                    SET last_verified=?, verification_count=?, updated_at=?
                    WHERE id=?
                """, (now_iso, new_ver_count, now_iso, cond_id))
                logger.info(f"  ✅ {ticker} unknown verdict '{verdict}' → treated as confirm")
                summary['confirmed'] += 1

            # Persist any data gaps the verifier identified (merge with existing analyst gaps)
            verifier_gaps = result.get('data_gaps') or []
            if verifier_gaps:
                _merge_and_store_data_gaps(conn, cond_id, verifier_gaps, cond.get('data_gaps_json'))

            conn.commit()

        except Exception as e:
            logger.error(f"  ❌ Verification failed for {ticker}: {e}", exc_info=True)
            summary['errors'] += 1

    conn.close()
    logger.info(
        f"✅ Verification cycle complete: "
        f"{summary['confirmed']} confirmed, "
        f"{summary['flagged']} flagged, "
        f"{summary['corrected']} corrected, "
        f"{summary['demoted']} demoted, "
        f"{summary['archived']} archived, "
        f"{summary['invalidated']} hard-invalidated, "
        f"{summary['errors']} errors"
    )
    return summary


if __name__ == '__main__':
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    print(f"\n🔍 Acquisition Verifier — manual run")
    summary = run_verification_cycle()
    print(f"\nResults: {summary}")
