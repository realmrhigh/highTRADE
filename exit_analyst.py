#!/usr/bin/env python3
"""
exit_analyst.py — Generates stop-loss and take-profit exit frameworks for
existing open positions that have no managed exit levels.

This is a SEPARATE analyst from the acquisition pipeline. The acquisition
pipeline asks "should we buy this?" — this module asks "given we already own
this, what are our exit levels?"

Flow:
  trade_records (status='open', stop_loss IS NULL)
      ↓ [Gemini balanced — focused exit-framework prompt]
  trade_records.stop_loss + take_profit_1 + take_profit_2  (written directly)
  + Slack alert to #hightrade

Guard: one run per open position per day (tracked via exit_analyst_log table).
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


def _already_ran_today(conn: sqlite3.Connection, trade_id: int) -> bool:
    """Return True if exit analyst already ran for this trade today."""
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
    """Build the Gemini exit-framework prompt."""
    entry    = pos['entry_price']
    current  = pos['current_price'] or entry
    shares   = pos['shares']
    pnl_pct  = ((current - entry) / entry * 100) if entry else 0
    pnl_dol  = (current - entry) * shares if shares else 0
    held_days = (datetime.now() - datetime.fromisoformat(pos['entry_date'])).days if pos.get('entry_date') else '?'

    r = research  # shorthand

    def _fmt(v, fmt='.2f', prefix='', suffix='', na='N/A'):
        return f"{prefix}{v:{fmt}}{suffix}" if v is not None else na

    prompt = f"""You are an exit-strategy analyst for a paper trading portfolio. You do NOT make entry recommendations — only exit levels for an EXISTING position.

POSITION:
  Ticker:        {pos['asset_symbol']}
  Entry price:   ${entry:.2f}
  Current price: ${current:.2f}
  Shares held:   {shares}
  Unrealized P&L: {pnl_pct:+.2f}% (${pnl_dol:+,.0f})
  Days held:     {held_days}
  DEFCON at entry: {pos.get('defcon_at_entry', '?')}

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

YOUR TASK:
Set a disciplined exit framework for this open position. You are managing risk for capital already deployed.

Consider:
1. Key support/resistance levels relative to current price
2. Earnings risk (proximity to next earnings date)
3. Macro regime — DEFCON {defcon} means {"extreme caution, tight stops" if defcon >= 4 else "moderate caution" if defcon == 3 else "constructive, give room to run"}
4. Whether the position is in profit or loss (affects stop strategy)
5. Analyst targets for take-profit anchoring

Output STRICT JSON only — no prose, no markdown:
{{
  "stop_loss": <float, absolute price, NOT a percentage>,
  "take_profit_1": <float, first exit target, absolute price>,
  "take_profit_2": <float or null, optional second target for partial exit>,
  "stop_rationale": "<1 sentence — why this stop level>",
  "tp_rationale": "<1 sentence — why these targets>",
  "risk_reward": "<e.g. 1:2.5>",
  "urgency": "immediate|watch|hold",
  "notes": "<any important context — earnings, catalysts, invalidation>"
}}
"""
    return prompt


def run_exit_analysis(
    defcon: int = 5,
    macro_score: float = 50.0,
    alerts=None
) -> list:
    """
    Main entry point. Scans for open positions with no stop/TP, runs Gemini
    exit analysis on each, writes results to trade_records, fires Slack alerts.

    Returns list of tickers that got exit frameworks set.
    """
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    _ensure_log_table(conn)

    # Find open positions with no managed exits
    unmanaged = conn.execute("""
        SELECT trade_id, asset_symbol, entry_price, current_price, shares,
               position_size_dollars, unrealized_pnl_dollars, unrealized_pnl_percent,
               entry_date, defcon_at_entry
        FROM trade_records
        WHERE status = 'open'
          AND (stop_loss IS NULL OR stop_loss = 0)
          AND (take_profit_1 IS NULL OR take_profit_1 = 0)
    """).fetchall()

    if not unmanaged:
        logger.info("✅ Exit analyst: all open positions have exit frameworks.")
        conn.close()
        return []

    logger.info(f"🎯 Exit analyst: {len(unmanaged)} unmanaged position(s) to analyze")
    processed = []

    for pos in unmanaged:
        pos = dict(pos)
        ticker = pos['asset_symbol']
        trade_id = pos['trade_id']

        # Guard — don't spam Gemini; once per 20 hours per position
        if _already_ran_today(conn, trade_id):
            logger.info(f"  ⏭️  {ticker}: exit analysis already ran today — skipping")
            continue

        logger.info(f"  🔍 Analyzing exit framework for {ticker} (trade_id={trade_id})")
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

        # Strip code fences if present
        if '```json' in text:
            text = text.split('```json')[1].split('```')[0].strip()
        elif '```' in text:
            text = text.split('```')[1].split('```')[0].strip()

        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            logger.error(f"  ❌ {ticker}: failed to parse exit framework JSON: {text[:200]}")
            continue

        stop  = result.get('stop_loss')
        tp1   = result.get('take_profit_1')
        tp2   = result.get('take_profit_2')
        stop_r = result.get('stop_rationale', '')
        tp_r   = result.get('tp_rationale', '')
        rr     = result.get('risk_reward', '?')
        urgency = result.get('urgency', 'watch')
        notes  = result.get('notes', '')

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

        # Write exit levels to trade_records
        conn.execute("""
            UPDATE trade_records
            SET stop_loss=?, take_profit_1=?, take_profit_2=?
            WHERE trade_id=?
        """, (stop, tp1, tp2, trade_id))

        # Log to exit_analyst_log (guard table)
        conn.execute("""
            INSERT INTO exit_analyst_log
            (trade_id, ticker, stop_loss, take_profit_1, take_profit_2, rationale, tokens_in, tokens_out)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (trade_id, ticker, stop, tp1, tp2,
              f"Stop: {stop_r} | TP: {tp_r}", tok_in, tok_out))

        conn.commit()
        logger.info(
            f"  ✅ {ticker}: exit framework set — stop=${stop:.2f}, TP1=${tp1:.2f}, TP2={f'${tp2:.2f}' if tp2 else 'None'} | R:R={rr}"
        )

        # Slack alert
        if alerts:
            urgency_emoji = '🚨' if urgency == 'immediate' else '⚠️' if urgency == 'watch' else '📌'
            alert_text = (
                f"{urgency_emoji} *Exit Framework Set: {ticker}*\n"
                f"Entry: ${entry:.2f} → Current: ${current:.2f} ({pnl_pct:+.1f}%)\n"
                f"🛑 Stop Loss: *${stop:.2f}*  — {stop_r}\n"
                f"🎯 TP1: *${tp1:.2f}*" + (f"   TP2: ${tp2:.2f}" if tp2 else '') + f"\n"
                f"📐 Risk/Reward: {rr}\n"
                f"💡 {notes}"
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
