#!/usr/bin/env python3
"""
exit_analyst.py — Generates stop-loss, take-profit, and catalyst exit frameworks
for existing open positions that are missing managed exit levels.

This is a SEPARATE analyst from the acquisition pipeline. The acquisition
pipeline asks "should we buy this?" — this module asks "given we already own
this, what are our exit levels and is there a catalyst driving this trade?"

Flow:
  trade_records (status='open', stop_loss IS NULL or catalyst_event IS NULL)
      ↓ [Gemini balanced — exit-framework + catalyst detection prompt]
  trade_records.stop_loss + take_profit_1 + take_profit_2  (written directly)
  trade_records.catalyst_event + catalyst_window_end + catalyst_spike_pct + catalyst_failure_pct
  + Slack alert to #hightrade

Guard: one run per open position per 20 hours (tracked via exit_analyst_log table).

Catalyst exit framework:
  If the position was entered on a specific event (product launch, earnings, FDA, macro):
    - catalyst_event: description of the event
    - catalyst_window_end: ISO timestamp when the window expires (entry + window_hours)
    - catalyst_spike_pct: sell into strength if price up ≥ this % from entry within window
    - catalyst_failure_pct: exit early if price down ≥ this % from entry within window
  If no catalyst detected, all four fields remain NULL (normal stop/TP applies).

Reference catalyst parameters (Gemini calibrates per event type):
  Product reveal / "sell the news" event (robotaxi, keynote, product drop):
                                            spike=1.5%, failure=-1.5%, window=24h
    → Low bar: we expect a modest 1-2% pop. If it doesn't materialise in 24h → exit.
  Sustained product launch (new product line with multi-day ramp):
                                            spike=3.0%, failure=-2.0%, window=48h
  Expected earnings beat:                   spike=4.0%, failure=-5.0%, window=24h
  FDA approval/rejection:                   spike=8.0%, failure=-5.0%, window=24h
  Fed/macro announcement:                   spike=2.0%, failure=-2.0%, window=6h
  General catalyst:                         spike=3.0%, failure=-2.0%, window=24h
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import gemini_client

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / 'trading_data' / 'trading_history.db'


def _ensure_log_table(conn: sqlite3.Connection):
    """Create the guard table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS exit_analyst_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id    INTEGER NOT NULL,
            ticker      TEXT NOT NULL,
            ran_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            stop_loss   REAL,
            take_profit_1 REAL,
            take_profit_2 REAL,
            rationale   TEXT,
            tokens_in   INTEGER DEFAULT 0,
            tokens_out  INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    # Idempotent migration: add data_gaps_json column if absent
    try:
        conn.execute("ALTER TABLE exit_analyst_log ADD COLUMN data_gaps_json TEXT")
        conn.commit()
    except Exception:
        pass  # Column already exists


def _already_ran_today(conn: sqlite3.Connection, trade_id: int) -> bool:
    """Return True if exit analyst already ran for this trade in the last 20 hours."""
    cutoff = (datetime.now() - timedelta(hours=20)).strftime('%Y-%m-%d %H:%M:%S')
    row = conn.execute(
        "SELECT id FROM exit_analyst_log WHERE trade_id=? AND ran_at > ?",
        (trade_id, cutoff)
    ).fetchone()
    return row is not None


def _fetch_research(conn: sqlite3.Connection, ticker: str) -> dict:
    """Pull the most recent research snapshot from stock_research_library."""
    conn.row_factory = sqlite3.Row
    row = conn.execute("""
        SELECT current_price, price_52w_high, price_52w_low,
               pe_ratio, forward_pe, profit_margin, revenue_growth_yoy,
               earnings_growth_yoy, debt_to_equity,
               analyst_target_mean, analyst_target_high, analyst_target_low,
               analyst_buy_count, analyst_hold_count, analyst_sell_count,
               next_earnings_date, last_eps_surprise_pct,
               news_mention_count, news_sentiment_avg,
               macro_score, market_regime,
               short_pct_float, options_atm_iv_call, options_put_call_ratio,
               vix_level, recommendation_key
        FROM stock_research_library
        WHERE ticker = ?
        ORDER BY id DESC LIMIT 1
    """, (ticker,)).fetchone()
    return dict(row) if row else {}


def _build_prompt(pos: dict, research: dict, defcon: int, macro_score: float) -> str:
    """Build the Gemini exit-framework + catalyst detection prompt."""
    entry     = pos['entry_price']
    current   = pos['current_price'] or entry
    shares    = pos['shares']
    pnl_pct   = ((current - entry) / entry * 100) if entry else 0
    pnl_dol   = (current - entry) * shares if shares else 0
    held_days = (datetime.now() - datetime.fromisoformat(pos['entry_date'])).days \
                if pos.get('entry_date') else '?'
    has_stop  = bool(pos.get('stop_loss'))
    has_tp    = bool(pos.get('take_profit_1'))
    notes_raw = pos.get('notes') or ''

    r = research

    def _fmt(v, fmt='.2f', prefix='', suffix='', na='N/A'):
        return f"{prefix}{v:{fmt}}{suffix}" if v is not None else na

    stop_section = ""
    if has_stop and has_tp:
        stop_section = (
            f"\nEXISTING EXIT LEVELS (already set — do NOT change these):\n"
            f"  Stop loss:    ${pos['stop_loss']:.2f}\n"
            f"  Take profit 1: ${pos['take_profit_1']:.2f}\n"
            + (f"  Take profit 2: ${pos['take_profit_2']:.2f}\n" if pos.get('take_profit_2') else '')
            + f"  → Reproduce these exactly in your JSON. Your only new task is CATALYST DETECTION.\n"
        )

    prompt = f"""You are an exit-strategy analyst for a paper trading portfolio. You do NOT make entry recommendations — only exit levels for an EXISTING position.

POSITION:
  Ticker:        {pos['asset_symbol']}
  Entry price:   ${entry:.2f}
  Current price: ${current:.2f}
  Shares held:   {shares}
  Unrealized P&L: {pnl_pct:+.2f}% (${pnl_dol:+,.0f})
  Days held:     {held_days}
  DEFCON at entry: {pos.get('defcon_at_entry', '?')}
  Entry notes:   {notes_raw or 'None'}
{stop_section}
CURRENT MACRO:
  DEFCON: {defcon}/5   Macro score: {_fmt(macro_score, '.1f')}/100
  VIX: {_fmt(r.get('vix_level'), '.1f')}   Market regime: {r.get('market_regime','?')}

FUNDAMENTALS (from latest research):
  52W High: {_fmt(r.get('price_52w_high'), '.2f', '$')}   52W Low: {_fmt(r.get('price_52w_low'), '.2f', '$')}
  P/E: {_fmt(r.get('pe_ratio'), '.1f')}   Fwd P/E: {_fmt(r.get('forward_pe'), '.1f')}
  Revenue growth YoY: {_fmt(r.get('revenue_growth_yoy'), '.1%', na='N/A') if r.get('revenue_growth_yoy') is not None else 'N/A'}
  Analyst target (mean/high/low): {_fmt(r.get('analyst_target_mean'), '.2f', '$')} / {_fmt(r.get('analyst_target_high'), '.2f', '$')} / {_fmt(r.get('analyst_target_low'), '.2f', '$')}
  Analyst sentiment: {r.get('analyst_buy_count','?')} buy / {r.get('analyst_hold_count','?')} hold / {r.get('analyst_sell_count','?')} sell  ({r.get('recommendation_key','?')})
  Next earnings: {r.get('next_earnings_date', 'Unknown')}
  Short % float: {_fmt(r.get('short_pct_float'), '.1%', na='N/A') if r.get('short_pct_float') is not None else 'N/A'}
  Options IV (ATM call): {_fmt(r.get('options_atm_iv_call'), '.1%', na='N/A') if r.get('options_atm_iv_call') is not None else 'N/A'}
  P/C ratio: {_fmt(r.get('options_put_call_ratio'), '.2f')}

{"YOUR TASK — EXIT LEVELS:" if not (has_stop and has_tp) else "YOUR TASK — CATALYST DETECTION ONLY:"}
{"Set a disciplined exit framework for this position. You are managing risk for capital already deployed." if not (has_stop and has_tp) else "Exit levels are already set. Focus only on the catalyst detection below."}

{"Consider:" if not (has_stop and has_tp) else ""}
{"1. Key support/resistance levels relative to current price" if not (has_stop and has_tp) else ""}
{"2. Earnings risk (proximity to next earnings date)" if not (has_stop and has_tp) else ""}
{"3. Macro regime — DEFCON " + str(defcon) + " means " + ("extreme caution, tight stops" if defcon >= 4 else "moderate caution" if defcon == 3 else "constructive, give room to run") if not (has_stop and has_tp) else ""}
{"4. Whether the position is in profit or loss (affects stop strategy)" if not (has_stop and has_tp) else ""}
{"5. Analyst targets for take-profit anchoring" if not (has_stop and has_tp) else ""}

CATALYST DETECTION (REQUIRED for all positions):
Determine whether this position was entered on a specific upcoming or recent event catalyst
(product launch, earnings event, FDA decision, regulatory event, macro announcement, etc.).

Look for clues in: entry notes, ticker identity, days held, earnings proximity, news context.

If a catalyst is detected, set the catalyst exit parameters:
  catalyst_event:        Brief name of the event (e.g. "Robotaxi product launch 2026-03-01")
  catalyst_window_hours: How many hours from ENTRY to wait for the move to materialize
  catalyst_spike_pct:    If price rises >= this % from entry WITHIN the window → sell into strength
                         (captures the catalyst gain before "buy the rumor, sell the news" reversal)
  catalyst_failure_pct:  If price falls >= this % from entry WITHIN the window → exit early
                         (catalyst going wrong direction = thesis failed, don't wait for stop)

Reference parameters by catalyst type (calibrate to the specific event):
  Product reveal / "sell the news" (robotaxi, keynote, product drop): spike=1.5, failure=-1.5, window=24
    IMPORTANT: For single-event reveals the bar is LOW — if price does not move >=1.5% within
    24h the market is underwhelming the news; exit via SELL_CATALYST_EXPIRED.
  Sustained product launch (new line, multi-day expected ramp):       spike=3.0, failure=-2.0, window=48
  Expected earnings beat:                                             spike=4.0, failure=-5.0, window=24
  FDA approval/rejection catalyst:                                    spike=8.0, failure=-5.0, window=24
  Fed/macro announcement:                                             spike=2.0, failure=-2.0, window=6
  General catalyst:                                                   spike=3.0, failure=-2.0, window=24

VOLATILITY SCALING — scale spike_pct to the stock's daily noise floor:
  These reference values assume a mid-volatility stock (beta ~1.0, ATM IV ~25-35%).
  For high-beta / high-IV stocks (TSLA, GME, NVDA — beta >1.5 or ATM IV >50%):
    → Multiply spike_pct and failure_pct by 1.5–2×
    → e.g. product reveal on TSLA → spike=2.5–3.0, failure=-2.5, window=24
    → Rationale: a 1.5% move is pure noise for a stock that swings 3% on a quiet day.
      The spike must EXCEED the stock's normal daily move to confirm the catalyst worked.
  For low-beta / low-IV stocks (utilities, staples — beta <0.8 or ATM IV <20%):
    → Use the reference values as-is or scale down slightly.

IMPORTANT: failure_pct is typically TIGHTER than the stop-loss during the catalyst window,
because a price decline during a "good news" event signals the thesis is already wrong.
Set all catalyst fields to null if no specific event catalyst is driving this trade.

Output STRICT JSON only — no prose, no markdown:
{{
  "stop_loss": <float, absolute price — reproduce existing if already set>,
  "take_profit_1": <float, first exit target — reproduce existing if already set>,
  "take_profit_2": <float or null>,
  "stop_rationale": "<1 sentence>",
  "tp_rationale": "<1 sentence>",
  "risk_reward": "<e.g. 1:2.5>",
  "urgency": "immediate|watch|hold",
  "notes": "<any important context — earnings, catalysts, invalidation>",
  "catalyst_event": "<event description or null>",
  "catalyst_window_hours": <integer or null>,
  "catalyst_spike_pct": <float or null>,
  "catalyst_failure_pct": <float — must be negative, e.g. -2.0 — or null>,
  "data_gaps": ["<specific data that was absent or stale and would have sharpened exit levels — e.g. 'live short interest', 'next earnings date not confirmed', 'options IV not available'>"]
}}
"""
    return prompt


def run_exit_analysis(
    defcon: int = 5,
    macro_score: float = 50.0,
    alerts=None
) -> list:
    """
    Main entry point. Scans for open positions missing stop/TP OR missing catalyst data.
    Runs Gemini exit analysis on each, writes results to trade_records, fires Slack alerts.

    Returns list of tickers that got frameworks set or updated.
    """
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    _ensure_log_table(conn)

    # Find open positions needing analysis:
    #   1. Missing stop/TP (always need full analysis)
    #   2. Have stop/TP but no catalyst data yet (catalyst detection pass)
    unmanaged = conn.execute("""
        SELECT trade_id, asset_symbol, entry_price, current_price, shares,
               position_size_dollars, unrealized_pnl_dollars, unrealized_pnl_percent,
               entry_date, defcon_at_entry, notes,
               stop_loss, take_profit_1, take_profit_2,
               catalyst_event, catalyst_window_end, catalyst_spike_pct, catalyst_failure_pct
        FROM trade_records
        WHERE status = 'open'
          AND (
            (stop_loss IS NULL OR stop_loss = 0)
            OR (take_profit_1 IS NULL OR take_profit_1 = 0)
            OR catalyst_event IS NULL
          )
    """).fetchall()

    if not unmanaged:
        logger.info("✅ Exit analyst: all open positions have exit frameworks and catalyst data.")
        conn.close()
        return []

    logger.info(f"🎯 Exit analyst: {len(unmanaged)} position(s) need analysis")
    processed = []

    for pos in unmanaged:
        pos = dict(pos)
        ticker   = pos['asset_symbol']
        trade_id = pos['trade_id']

        # Guard — don't spam Gemini; once per 20 hours per position
        if _already_ran_today(conn, trade_id):
            logger.info(f"  ⏭️  {ticker}: exit analysis already ran today — skipping")
            continue

        has_stop = bool(pos.get('stop_loss'))
        has_tp   = bool(pos.get('take_profit_1'))
        needs_catalyst = pos.get('catalyst_event') is None

        task = ('stop/TP + catalyst' if not (has_stop and has_tp) else 'catalyst detection')
        logger.info(f"  🔍 Analyzing {ticker} (trade_id={trade_id}) — {task}")

        research = _fetch_research(conn, ticker)
        prompt   = _build_prompt(pos, research, defcon, macro_score)

        text, tok_in, tok_out = gemini_client.call(
            prompt,
            model_key='balanced',
            caller='exit_analyst',
        )

        if not text:
            logger.warning(f"  ⚠️  {ticker}: Gemini returned empty response")
            continue

        # Strip code fences
        if '```json' in text:
            text = text.split('```json')[1].split('```')[0].strip()
        elif '```' in text:
            text = text.split('```')[1].split('```')[0].strip()

        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            logger.error(f"  ❌ {ticker}: failed to parse exit framework JSON: {text[:200]}")
            continue

        stop   = result.get('stop_loss') or pos.get('stop_loss')
        tp1    = result.get('take_profit_1') or pos.get('take_profit_1')
        tp2    = result.get('take_profit_2') or pos.get('take_profit_2')
        stop_r = result.get('stop_rationale', '')
        tp_r   = result.get('tp_rationale', '')
        rr     = result.get('risk_reward', '?')
        urgency = result.get('urgency', 'watch')
        notes  = result.get('notes', '')

        # Catalyst fields
        cat_event   = result.get('catalyst_event')        # str or None
        cat_hours   = result.get('catalyst_window_hours') # int or None
        cat_spike   = result.get('catalyst_spike_pct')    # float or None
        cat_fail    = result.get('catalyst_failure_pct')  # float (negative) or None

        if not stop or not tp1:
            logger.warning(f"  ⚠️  {ticker}: incomplete exit framework (stop={stop}, tp1={tp1})")
            continue

        entry   = pos['entry_price']
        current = pos['current_price'] or entry
        pnl_pct = ((current - entry) / entry * 100) if entry else 0

        # Sanity check — stop must be below current price for longs
        if stop >= current:
            logger.warning(f"  ⚠️  {ticker}: stop_loss {stop:.2f} >= current {current:.2f} — skipping (invalid)")
            continue

        # Compute catalyst_window_end from entry_date + window_hours
        cat_window_end = None
        if cat_event and cat_hours:
            try:
                entry_dt = datetime.fromisoformat(pos['entry_date'][:19])
                cat_window_end = (entry_dt + timedelta(hours=cat_hours)).isoformat()
            except Exception:
                cat_window_end = (datetime.now() + timedelta(hours=cat_hours)).isoformat()

        # Write exit levels + catalyst data to trade_records
        conn.execute("""
            UPDATE trade_records
            SET stop_loss=?, take_profit_1=?, take_profit_2=?,
                catalyst_event=?, catalyst_window_end=?,
                catalyst_spike_pct=?, catalyst_failure_pct=?
            WHERE trade_id=?
        """, (stop, tp1, tp2,
              cat_event, cat_window_end, cat_spike, cat_fail,
              trade_id))

        # Log to guard table
        exit_gaps = result.get('data_gaps') or []
        conn.execute("""
            INSERT INTO exit_analyst_log
            (trade_id, ticker, stop_loss, take_profit_1, take_profit_2, rationale,
             tokens_in, tokens_out, data_gaps_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (trade_id, ticker, stop, tp1, tp2,
              f"Stop: {stop_r} | TP: {tp_r}", tok_in, tok_out,
              json.dumps(exit_gaps) if exit_gaps else None))

        conn.commit()

        catalyst_line = ''
        if cat_event:
            window_end_str = cat_window_end[:16] if cat_window_end else '?'
            catalyst_line = (
                f"\n  🎯 Catalyst: {cat_event}\n"
                f"     Window: until {window_end_str} | "
                f"Sell spike ≥{cat_spike}% | Exit if ≤{cat_fail}%"
            )
            logger.info(
                f"  📅 {ticker}: catalyst detected — '{cat_event}' | "
                f"window={cat_hours}h, spike={cat_spike}%, failure={cat_fail}%"
            )
        else:
            logger.info(f"  🔍 {ticker}: no event catalyst detected — normal stop/TP applies")

        logger.info(
            f"  ✅ {ticker}: exit framework set — stop=${stop:.2f}, "
            f"TP1=${tp1:.2f}, TP2={f'${tp2:.2f}' if tp2 else 'None'} | R:R={rr}"
        )

        # Slack alert
        if alerts:
            urgency_emoji = '🚨' if urgency == 'immediate' else '⚠️' if urgency == 'watch' else '📌'
            alert_text = (
                f"{urgency_emoji} *Exit Framework Set: {ticker}*\n"
                f"Entry: ${entry:.2f} → Current: ${current:.2f} ({pnl_pct:+.1f}%)\n"
                f"🛑 Stop Loss: *${stop:.2f}*  — {stop_r}\n"
                f"🎯 TP1: *${tp1:.2f}*" + (f"   TP2: ${tp2:.2f}" if tp2 else '') + "\n"
                f"📐 Risk/Reward: {rr}"
                + catalyst_line
                + (f"\n💡 {notes}" if notes else '')
            )
            try:
                alerts.send_slack_alert(alert_text, channel='#hightrade')
            except Exception as e:
                logger.warning(f"  Slack alert failed for {ticker}: {e}")

        processed.append(ticker)

    conn.close()
    return processed


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    result = run_exit_analysis(defcon=5, macro_score=57.0)
    print(f"Exit frameworks set for: {result}")
