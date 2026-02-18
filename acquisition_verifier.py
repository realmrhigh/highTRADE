#!/usr/bin/env python3
"""
acquisition_verifier.py — Daily Flash reverification of active conditionals.

Runs once per day (called by daily_briefing.py after the main briefing).
For each 'active' conditional in conditional_tracking, it feeds a compact
snapshot of current price, recent news, and macro to Gemini Flash (fast tier,
no thinking budget) and asks: confirm / flag / invalidate.

  confirm    → update last_verified, increment verification_count
  flag       → status stays 'active' but verification_notes records the concern
               Analyst should review flagged conditionals manually
  invalidate → status = 'invalidated', thesis has failed

This is intentionally a cheap call (Flash, no thinking) because it runs on
potentially many conditionals daily.
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import gemini_client

logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent.resolve()
DB_PATH    = SCRIPT_DIR / 'trading_data' / 'trading_history.db'

_VERIFIER_JSON_TEMPLATE = """{
  "verdict": "confirm",
  "confidence_adjustment": 0.0,
  "flag_reason": "",
  "invalidation_reason": "",
  "updated_thesis": "",
  "price_still_valid": true,
  "reasoning": "brief explanation"
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
                            recent_news: List[str], macro: Dict) -> str:
    """Build the compact Flash verification prompt."""
    ticker      = cond['ticker']
    entry_tgt   = cond.get('entry_price_target', 'N/A')
    stop        = cond.get('stop_loss', 'N/A')
    tp1         = cond.get('take_profit_1', 'N/A')
    thesis      = cond.get('thesis_summary', 'N/A')
    confidence  = cond.get('research_confidence', 0)
    date_set    = cond.get('date_created', 'N/A')
    conditions  = json.loads(cond.get('entry_conditions_json') or '[]')
    invalidates = json.loads(cond.get('invalidation_conditions_json') or '[]')

    price_str   = f"${current_price:.2f}" if current_price else 'N/A'
    distance    = None
    if current_price and isinstance(entry_tgt, (int, float)):
        distance = (current_price - entry_tgt) / entry_tgt * 100
    distance_str = f"{distance:+.1f}% from entry target" if distance is not None else ''

    news_text = '\n'.join(f"  • {n}" for n in recent_news) if recent_news else '  • No recent mentions'
    cond_text = '\n'.join(f"  • {c}" for c in conditions[:3]) if conditions else '  • N/A'
    inv_text  = '\n'.join(f"  • {c}" for c in invalidates[:2]) if invalidates else '  • N/A'

    macro_text = ''
    if macro:
        macro_text = (
            f"  Macro score: {macro.get('macro_score', 'N/A')}\n"
            f"  Yield curve: {macro.get('yield_curve_spread', 'N/A'):+.2f}% " if isinstance(macro.get('yield_curve_spread'), float) else
            f"  Macro score: {macro.get('macro_score', 'N/A')}\n"
        )

    return (
        f"You are a trading system verifier. Today is {datetime.now().strftime('%Y-%m-%d')}.\n"
        f"A Gemini 3 Pro analyst set a conditional entry on {ticker} on {date_set}.\n"
        f"Your job: quickly decide if this conditional is still VALID.\n\n"
        f"CONDITIONAL SUMMARY\n"
        f"  Thesis: {thesis}\n"
        f"  Entry target: ${entry_tgt}  |  Stop: ${stop}  |  TP1: ${tp1}\n"
        f"  Original confidence: {confidence:.2f}\n\n"
        f"ENTRY CONDITIONS\n{cond_text}\n\n"
        f"INVALIDATION TRIGGERS\n{inv_text}\n\n"
        f"CURRENT STATE ({datetime.now().strftime('%Y-%m-%d')})\n"
        f"  Current price: {price_str} {distance_str}\n"
        f"{macro_text}\n"
        f"RECENT NEWS MENTIONS\n{news_text}\n\n"
        f"VERDICT OPTIONS:\n"
        f"  confirm    — thesis intact, conditional still valid, nothing has changed materially\n"
        f"  flag       — something concerns me, analyst should review, but don't kill it yet\n"
        f"  invalidate — a core invalidation condition has been triggered or thesis has clearly failed\n\n"
        f"Respond ONLY in this exact JSON format:\n"
        f"{_VERIFIER_JSON_TEMPLATE}"
    )


def _parse_verifier_response(text: str) -> Dict:
    """Parse Flash JSON response."""
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        return {'verdict': 'confirm', 'reasoning': 'parse_failed', '_parse_failed': True}


def run_verification_cycle() -> Dict:
    """
    Main entry point — called by daily_briefing.py after the main briefing.

    Iterates all 'active' conditionals, runs Flash verification on each,
    and updates conditional_tracking accordingly.

    Returns summary dict: {'confirmed': n, 'flagged': n, 'invalidated': n, 'errors': n}
    """
    date_str = datetime.now().strftime('%Y-%m-%d')
    logger.info(f"🔍 Acquisition Verifier: starting cycle for {date_str}")

    conn = _get_conn()
    summary = {'confirmed': 0, 'flagged': 0, 'invalidated': 0, 'errors': 0}

    try:
        cursor = conn.execute("""
            SELECT id, ticker, date_created, entry_price_target, stop_loss,
                   take_profit_1, take_profit_2, thesis_summary, research_confidence,
                   entry_conditions_json, invalidation_conditions_json,
                   verification_count, time_horizon_days
            FROM conditional_tracking
            WHERE status = 'active'
            ORDER BY research_confidence DESC
        """)
        actives = [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Failed to fetch active conditionals: {e}")
        conn.close()
        return summary

    if not actives:
        logger.info("  📭 No active conditionals to verify")
        conn.close()
        return summary

    logger.info(f"  📋 {len(actives)} active conditionals to verify: {[c['ticker'] for c in actives]}")
    macro = _get_latest_macro(conn)

    for cond in actives:
        ticker  = cond['ticker']
        cond_id = cond['id']

        logger.info(f"  🔎 Verifying {ticker}...")

        try:
            current_price = _get_current_price(ticker)
            recent_news   = _fetch_recent_news_for_ticker(ticker, conn)
            prompt        = _build_verifier_prompt(cond, current_price, recent_news, macro)

            text, in_tok, out_tok = gemini_client.call(
                prompt=prompt,
                model_key='fast',   # No thinking — cheap and fast
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

            now_iso = datetime.now().isoformat()
            new_count = (cond.get('verification_count') or 0) + 1

            if verdict == 'invalidate':
                conn.execute("""
                    UPDATE conditional_tracking
                    SET status='invalidated', verification_notes=?, last_verified=?,
                        verification_count=?, updated_at=?
                    WHERE id=?
                """, (
                    result.get('invalidation_reason', result.get('reasoning', '')),
                    now_iso, new_count, now_iso, cond_id
                ))
                summary['invalidated'] += 1
                logger.info(f"  ❌ {ticker} INVALIDATED: {result.get('invalidation_reason','')}")

                # Update acquisition_watchlist status
                conn.execute("""
                    UPDATE acquisition_watchlist SET status='invalidated'
                    WHERE UPPER(ticker) = UPPER(?) AND status IN ('conditional_set','researched')
                """, (ticker,))

            elif verdict == 'flag':
                conn.execute("""
                    UPDATE conditional_tracking
                    SET verification_notes=?, last_verified=?,
                        verification_count=?, updated_at=?
                    WHERE id=?
                """, (
                    f"[FLAGGED {date_str}] {result.get('flag_reason', result.get('reasoning', ''))}",
                    now_iso, new_count, now_iso, cond_id
                ))
                summary['flagged'] += 1
                logger.warning(f"  🚩 {ticker} FLAGGED: {result.get('flag_reason','')}")

            else:  # confirm (or anything unexpected)
                conn.execute("""
                    UPDATE conditional_tracking
                    SET last_verified=?, verification_count=?, updated_at=?
                    WHERE id=?
                """, (now_iso, new_count, now_iso, cond_id))
                summary['confirmed'] += 1
                logger.info(f"  ✅ {ticker} confirmed valid")

            conn.commit()

        except Exception as e:
            logger.error(f"  ❌ Verification failed for {ticker}: {e}")
            summary['errors'] += 1

    conn.close()
    logger.info(
        f"✅ Verification cycle complete: "
        f"{summary['confirmed']} confirmed, "
        f"{summary['flagged']} flagged, "
        f"{summary['invalidated']} invalidated, "
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
