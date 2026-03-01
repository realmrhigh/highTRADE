#!/usr/bin/env python3
"""
acquisition_analyst.py — AI-powered conditional entry analysis.

Flow:
  stock_research_library (status='library_ready' or 'partial')
      ↓ [this module — Gemini 3 Pro with dynamic thinking]
  conditional_tracking (status='active')  if confidence >= CONFIDENCE_THRESHOLD
  else → stock_research_library.status = 'analyst_pass' (below threshold, skip)

The analyst reads all gathered research and asks:
  "Given everything we know, should we set a conditional entry order on this stock?
   If yes, specify exact price levels, position size, and the conditions that must
   be true at entry time."

Only trades with research_confidence >= 0.7 get promoted to the broker.
Position sizing: position_pct * available_cash, capped at MAX_POSITION_PCT.
"""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import gemini_client

logger = logging.getLogger(__name__)

SCRIPT_DIR          = Path(__file__).parent.resolve()
DB_PATH             = SCRIPT_DIR / 'trading_data' / 'trading_history.db'
CONFIDENCE_THRESHOLD = 0.70   # minimum to promote to broker
MAX_POSITION_PCT    = 0.20    # hard cap: max 20% of capital in any single trade


# ── Prompt template ────────────────────────────────────────────────────────────

_ANALYST_JSON_TEMPLATE = """{
  "should_enter": true,
  "research_confidence": 0.0,
  "watch_tag": "breakout",
  "watch_tag_rationale": "why this specific tag fits this setup",
  "entry_price_target": 0.0,
  "entry_price_rationale": "why this specific price",
  "stop_loss": 0.0,
  "stop_loss_rationale": "why this stop level",
  "take_profit_1": 0.0,
  "take_profit_2": 0.0,
  "take_profit_rationale": "why these targets",
  "position_size_pct": 0.0,
  "position_size_rationale": "why this size given risk profile",
  "time_horizon_days": 0,
  "entry_conditions": [
    "specific condition 1 that must be true before buying",
    "specific condition 2",
    "specific condition 3"
  ],
  "invalidation_conditions": [
    "condition that would invalidate this thesis entirely",
    "another invalidation trigger"
  ],
  "thesis_summary": "2-3 sentence explanation of why this trade makes sense NOW",
  "key_risks": ["risk1", "risk2", "risk3"],
  "macro_alignment": "how macro environment supports or contradicts this trade",
  "reasoning_chain": "step-by-step walk through of how you arrived at these levels",
  "data_gaps": ["<specific missing data that would improve entry precision or confidence — e.g. 'options open interest at $460 strike', 'insider buying last 30 days', 'next earnings date not in research'>"]
}"""

# Watch tag definitions injected into every analyst prompt
_WATCH_TAG_DEFINITIONS = """
══════════════════════════════════════════════════════
WATCH TAGS — assign exactly ONE to this trade
══════════════════════════════════════════════════════
Choose the tag that best describes the SETUP TYPE. It shapes your entry, sizing, and conditions.

  breakout       — Price testing or clearing a key resistance level (52w high, prior pivot).
                   Entry: above resistance. Stop: tight below breakout. Conditions: volume + momentum confirmation.

  mean-reversion — Overextended pullback to known support; expecting bounce back to mean.
                   Entry: at support. Stop: wider (below support). Conditions: oversold signal, no trend breakdown.

  momentum       — Strong established trend; adding on a healthy pullback.
                   Entry: near moving average or recent base. Stop: momentum-based. Conditions: trend intact.

  defensive-hedge — Risk-off asset (TLT, GLD, utilities) held during macro uncertainty.
                   Entry: any weakness. Stop: wide. Size: small (portfolio insurance, not profit center).
                   Conditions: macro score, VIX environment.

  macro-hedge    — Inverse or volatility instrument (SQQQ, VIX products, short ETFs).
                   Entry: strict — only when VIX > threshold AND DEFCON elevated.
                   Stop: tight. Size: smaller. Conditions: must include specific VIX/DEFCON gate.

  earnings-play  — Setup driven by an upcoming earnings catalyst.
                   Entry: before event date. Stop: wider. Conditions: earnings date, consensus vs expected.
                   Time horizon: short (exit after announcement).

  rebound        — Post-stop-loss recovery attempt on a previously held ticker.
                   Entry: on bottoming signal. Stop: tight. Size: reduced (half of normal).
                   Conditions: must see exhaustion of selling before re-entry.
"""


def _build_analyst_prompt(ticker: str, research: Dict) -> str:
    """Build the Gemini 3 Pro prompt from gathered research data."""

    # Price context
    price      = research.get('current_price', 'N/A')
    chg_1w     = research.get('price_1w_chg_pct')
    chg_1m     = research.get('price_1m_chg_pct')
    high_52w   = research.get('price_52w_high', 'N/A')
    low_52w    = research.get('price_52w_low', 'N/A')
    regime     = research.get('market_regime', 'unknown')

    price_block = (
        f"  Current price:  ${price}\n"
        f"  1-week change:  {f'{chg_1w:+.1f}%' if chg_1w is not None else 'N/A'}\n"
        f"  1-month change: {f'{chg_1m:+.1f}%' if chg_1m is not None else 'N/A'}\n"
        f"  52w High/Low:   ${high_52w} / ${low_52w}\n"
        f"  Market regime:  {regime}\n"
    )

    # Fundamental snapshot
    pe          = research.get('pe_ratio', 'N/A')
    fpe         = research.get('forward_pe', 'N/A')
    peg         = research.get('peg_ratio', 'N/A')
    pb          = research.get('price_to_book', 'N/A')
    margin      = research.get('profit_margin')
    rev_growth  = research.get('revenue_growth_yoy')
    earn_growth = research.get('earnings_growth_yoy')
    d2e         = research.get('debt_to_equity', 'N/A')
    fcf         = research.get('free_cash_flow')
    mcap        = research.get('market_cap')

    margin_str     = f"{margin*100:.1f}%" if isinstance(margin, float) else 'N/A'
    rev_str        = f"{rev_growth*100:+.1f}%" if isinstance(rev_growth, float) else 'N/A'
    earn_str       = f"{earn_growth*100:+.1f}%" if isinstance(earn_growth, float) else 'N/A'
    fcf_str        = f"${fcf/1e9:.2f}B" if isinstance(fcf, (int, float)) and fcf else 'N/A'
    mcap_str       = f"${mcap/1e9:.1f}B" if isinstance(mcap, (int, float)) and mcap else 'N/A'

    fundamentals_block = (
        f"  Market cap:       {mcap_str}\n"
        f"  P/E (trailing):   {pe}\n"
        f"  P/E (forward):    {fpe}\n"
        f"  PEG ratio:        {peg}\n"
        f"  Price/Book:       {pb}\n"
        f"  Profit margin:    {margin_str}\n"
        f"  Revenue growth:   {rev_str} YoY\n"
        f"  Earnings growth:  {earn_str} YoY\n"
        f"  Debt/Equity:      {d2e}\n"
        f"  Free cash flow:   {fcf_str}\n"
    )

    # Analyst consensus
    tgt_mean = research.get('analyst_target_mean')
    tgt_high = research.get('analyst_target_high')
    tgt_low  = research.get('analyst_target_low')
    buy_c    = research.get('analyst_buy_count', 0)
    hold_c   = research.get('analyst_hold_count', 0)
    sell_c   = research.get('analyst_sell_count', 0)
    upside   = ((tgt_mean - price) / price * 100) if (isinstance(tgt_mean, float) and isinstance(price, float)) else None

    analyst_block = (
        f"  Price targets:    Mean ${tgt_mean} / High ${tgt_high} / Low ${tgt_low}\n"
        f"  Implied upside:   {f'{upside:+.1f}%' if upside is not None else 'N/A'}\n"
        f"  Ratings (recent): {buy_c} Buy / {hold_c} Hold / {sell_c} Sell\n"
    )

    # Earnings
    next_earn    = research.get('next_earnings_date', 'Unknown')
    eps_surprise = research.get('last_eps_surprise_pct')
    eps_str      = f"{eps_surprise:+.1f}%" if isinstance(eps_surprise, float) else 'N/A'

    earnings_block = (
        f"  Next earnings:  {next_earn}\n"
        f"  Last EPS surprise: {eps_str}\n"
    )

    # SEC filings
    filing_type = research.get('latest_filing_type', 'N/A')
    filing_date = research.get('latest_filing_date', 'N/A')
    sec_8k      = research.get('sec_recent_8k_summary', 'None')

    sec_block = (
        f"  Latest filing:  {filing_type} on {filing_date}\n"
        f"  Recent 8-K:     {sec_8k or 'None'}\n"
    )

    # Internal signals
    news_count   = research.get('news_mention_count', 0)
    news_sent    = research.get('news_sentiment_avg')
    cong_strength = research.get('congressional_signal_strength', 0)
    cong_buys    = research.get('congressional_buy_count', 0)
    macro_score  = research.get('macro_score', 'N/A')

    signals_block = (
        f"  News mentions (30d): {news_count}\n"
        f"  News sentiment avg:  {f'{news_sent:.1f}/100' if isinstance(news_sent, float) else 'N/A'}\n"
        f"  Congressional signal strength: {cong_strength:.0f}\n"
        f"  Congressional buy count:       {cong_buys}\n"
        f"  Macro composite score:         {macro_score}\n"
    )

    import gemini_client as _gc
    _session_block = _gc.market_context_block()

    return (
        f"You are HighTrade's senior acquisition analyst. Today is {datetime.now().strftime('%Y-%m-%d')}.\n"
        f"You have been given comprehensive research on {ticker} gathered from multiple sources.\n"
        f"Your job: determine whether to set a CONDITIONAL ENTRY ORDER on {ticker}.\n\n"
        f"This is a paper trading system. Be precise and specific — no vague answers.\n"
        f"If you recommend entering, every price level must be a real number.\n\n"
        f"{_session_block}\n"
        f"══════════════════════════════════════════════════════\n"
        f"PRICE & TECHNICALS — {ticker}\n"
        f"══════════════════════════════════════════════════════\n"
        f"{price_block}\n"
        f"══════════════════════════════════════════════════════\n"
        f"FUNDAMENTALS\n"
        f"══════════════════════════════════════════════════════\n"
        f"{fundamentals_block}\n"
        f"══════════════════════════════════════════════════════\n"
        f"ANALYST CONSENSUS\n"
        f"══════════════════════════════════════════════════════\n"
        f"{analyst_block}\n"
        f"══════════════════════════════════════════════════════\n"
        f"EARNINGS\n"
        f"══════════════════════════════════════════════════════\n"
        f"{earnings_block}\n"
        f"══════════════════════════════════════════════════════\n"
        f"SEC FILINGS\n"
        f"══════════════════════════════════════════════════════\n"
        f"{sec_block}\n"
        f"══════════════════════════════════════════════════════\n"
        f"INTERNAL INTELLIGENCE SIGNALS\n"
        f"══════════════════════════════════════════════════════\n"
        f"{signals_block}\n"
        f"{_WATCH_TAG_DEFINITIONS}\n"
        f"══════════════════════════════════════════════════════\n"
        f"YOUR TASK\n"
        f"══════════════════════════════════════════════════════\n"
        f"Analyze ALL of the above. Decide:\n"
        f"  1. Which watch_tag fits this setup? (see definitions above)\n"
        f"  2. Should we set a conditional entry order? (true/false)\n"
        f"  3. At what exact price should we enter? (informed by your watch_tag)\n"
        f"  4. Where is the stop loss? (must be a specific price, not a percentage)\n"
        f"  5. Where are the take-profit targets? (TP1 for partial exit, TP2 for full)\n"
        f"  6. What % of available cash? (0.0–{MAX_POSITION_PCT:.2f}, sized per your watch_tag guidance)\n"
        f"  7. What specific, VERIFIABLE conditions must be TRUE at the time of entry?\n"
        f"     (Include numeric thresholds wherever possible: VIX < X, macro_score > Y, etc.)\n"
        f"  8. What would invalidate this thesis entirely?\n\n"
        f"Set research_confidence as a float 0.0–1.0 based on how convinced you are.\n"
        f"Only set should_enter=true if research_confidence >= {CONFIDENCE_THRESHOLD:.1f}.\n\n"
        f"Respond in this EXACT JSON format (no other text):\n"
        f"{_ANALYST_JSON_TEMPLATE}"
    )


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_conditional_table(conn: sqlite3.Connection):
    """Create conditional_tracking table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conditional_tracking (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker                  TEXT NOT NULL,
            date_created            TEXT NOT NULL,
            entry_price_target      REAL,
            entry_price_rationale   TEXT,
            stop_loss               REAL,
            stop_loss_rationale     TEXT,
            take_profit_1           REAL,
            take_profit_2           REAL,
            take_profit_rationale   TEXT,
            position_size_pct       REAL,
            position_size_rationale TEXT,
            time_horizon_days       INTEGER,
            entry_conditions_json   TEXT,
            invalidation_conditions_json TEXT,
            thesis_summary          TEXT,
            key_risks_json          TEXT,
            macro_alignment         TEXT,
            reasoning_chain         TEXT,
            research_confidence     REAL,
            -- Lifecycle
            status                  TEXT DEFAULT 'active',
            -- active → broker is watching this
            -- triggered → broker entered the position
            -- invalidated → thesis failed, archived
            -- flagged → Flash verifier raised concerns, needs analyst review
            -- expired → time horizon passed without trigger
            last_verified           TEXT,
            verification_count      INTEGER DEFAULT 0,
            verification_notes      TEXT,
            created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cond_ticker ON conditional_tracking(ticker)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cond_status ON conditional_tracking(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cond_date   ON conditional_tracking(date_created)")
    conn.commit()

    # Migrate: add watch_tag columns if they don't exist yet (SQLite has no IF NOT EXISTS for columns)
    for col, coltype in [('watch_tag', 'TEXT'), ('watch_tag_rationale', 'TEXT'),
                         ('attention_score', 'REAL'), ('attention_updated_at', 'TEXT')]:
        try:
            conn.execute(f"ALTER TABLE conditional_tracking ADD COLUMN {col} {coltype}")
            conn.commit()
        except Exception:
            pass  # Column already exists


def _parse_analyst_response(text: str) -> Dict:
    """Parse JSON from analyst model response."""
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    if "<think>" in text:
        parts = text.split("</think>")
        if len(parts) > 1:
            text = parts[-1].strip()

    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        # Attempt bracket repair
        for end in range(len(text), 0, -1):
            candidate = text[:end]
            opens = candidate.count('{') - candidate.count('}')
            if opens > 0:
                repaired = candidate.rstrip(',\n ') + ('}' * opens)
                try:
                    return json.loads(repaired)
                except json.JSONDecodeError:
                    continue
        return {'should_enter': False, 'research_confidence': 0.0, '_parse_failed': True}


# ── Analyst core ───────────────────────────────────────────────────────────────

def analyze_ticker(ticker: str, research: Dict, conn: sqlite3.Connection) -> Optional[Dict]:
    """
    Run Gemini 3 Pro analysis on a researched ticker.
    Returns the parsed analyst result dict, or None on failure.
    Writes to conditional_tracking if confidence >= threshold.
    Updates stock_research_library status.
    """
    date_str = datetime.now().strftime('%Y-%m-%d')
    logger.info(f"  🧠 Analyzing {ticker} with Gemini 3 Pro (dynamic thinking)...")

    prompt = _build_analyst_prompt(ticker, research)

    # ── Quota pre-check: downgrade to balanced if Pro is near its soft limit ──
    quota_status = gemini_client.check_quota('reasoning')
    if quota_status == 'block':
        logger.warning(f"  ⚠️  Pro quota near limit ({gemini_client.QUOTA_BLOCK_PCT*100:.0f}%+) — downgrading {ticker} to balanced tier")
        effective_model_key = 'balanced'
    elif quota_status == 'warn':
        logger.warning(f"  ⚠️  Pro quota at {gemini_client.QUOTA_WARN_PCT*100:.0f}%+ — monitoring ({ticker})")
        effective_model_key = 'reasoning'
    else:
        effective_model_key = 'reasoning'

    try:
        text, in_tok, out_tok = gemini_client.call(
            prompt=prompt,
            model_key=effective_model_key,
            caller='analyst',
        )
    except Exception as e:
        logger.error(f"  ❌ Gemini call failed for {ticker}: {e}")
        conn.execute("""
            UPDATE stock_research_library SET status = 'analyst_error', error_notes = ?
            WHERE ticker = ? AND status IN ('library_ready','partial')
        """, (str(e), ticker))
        conn.commit()
        return None

    if not text:
        logger.warning(f"  ⚠️  Empty response from Gemini for {ticker}")
        conn.execute("""
            UPDATE stock_research_library SET status = 'analyst_error', error_notes = 'Empty response'
            WHERE ticker = ? AND status IN ('library_ready','partial')
        """, (ticker,))
        conn.commit()
        return None

    result = _parse_analyst_response(text)
    result['_ticker']        = ticker
    result['_model']         = 'gemini-3-pro-preview'
    result['_input_tokens']  = in_tok
    result['_output_tokens'] = out_tok

    confidence   = float(result.get('research_confidence', 0.0))
    should_enter = result.get('should_enter', False)

    logger.info(
        f"  📊 {ticker}: should_enter={should_enter}, confidence={confidence:.2f} "
        f"({in_tok}→{out_tok} tok)"
    )

    # ── Write to conditional_tracking if above threshold ─────────────────
    if should_enter and confidence >= CONFIDENCE_THRESHOLD:
        try:
            # Expire any prior active conditionals for this ticker before inserting new one
            conn.execute("""
                UPDATE conditional_tracking
                SET status = 'invalidated',
                    verification_notes = 'Superseded by fresh analyst run on ' || date('now'),
                    updated_at = CURRENT_TIMESTAMP
                WHERE ticker = ? AND status = 'active'
            """, (ticker,))
            gaps = result.get('data_gaps', [])
            conn.execute("""
                INSERT OR REPLACE INTO conditional_tracking (
                    ticker, date_created,
                    entry_price_target, entry_price_rationale,
                    stop_loss, stop_loss_rationale,
                    take_profit_1, take_profit_2, take_profit_rationale,
                    position_size_pct, position_size_rationale,
                    time_horizon_days,
                    entry_conditions_json, invalidation_conditions_json,
                    thesis_summary, key_risks_json,
                    macro_alignment, reasoning_chain,
                    research_confidence,
                    watch_tag, watch_tag_rationale,
                    data_gaps_json,
                    status, last_verified
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?)
            """, (
                ticker, date_str,
                result.get('entry_price_target'),
                result.get('entry_price_rationale'),
                result.get('stop_loss'),
                result.get('stop_loss_rationale'),
                result.get('take_profit_1'),
                result.get('take_profit_2'),
                result.get('take_profit_rationale'),
                min(float(result.get('position_size_pct', 0.05)), MAX_POSITION_PCT),
                result.get('position_size_rationale'),
                result.get('time_horizon_days'),
                json.dumps(result.get('entry_conditions', [])),
                json.dumps(result.get('invalidation_conditions', [])),
                result.get('thesis_summary'),
                json.dumps(result.get('key_risks', [])),
                result.get('macro_alignment'),
                result.get('reasoning_chain'),
                confidence,
                result.get('watch_tag', 'mean-reversion'),  # default if Gemini omits
                result.get('watch_tag_rationale', ''),
                json.dumps(gaps) if gaps else None,
                date_str,
            ))
            conn.commit()
            logger.info(
                f"  ✅ {ticker} CONDITIONAL SET [{result.get('watch_tag','?')}]: "
                f"entry=${result.get('entry_price_target')}, "
                f"stop=${result.get('stop_loss')}, "
                f"TP1=${result.get('take_profit_1')}, "
                f"size={result.get('position_size_pct',0)*100:.0f}% of cash"
            )
            if gaps:
                logger.info(f"  🔍 Data gaps ({ticker}): {' | '.join(gaps)}")

            # Build rich thesis text and write back to watchlist thesis column
            thesis      = result.get('thesis_summary', '') or ''
            price_tgt   = result.get('entry_price_target')
            stop        = result.get('stop_loss')
            conds       = result.get('entry_conditions', []) or []
            cond_str    = ' | '.join(conds[:2]) if conds else ''
            thesis_text = thesis
            if price_tgt: thesis_text += f" ◆ Entry: ${price_tgt:.2f}"
            if stop:      thesis_text += f" / Stop: ${stop:.2f}"
            if cond_str:  thesis_text += f" ◆ {cond_str}"

            conn.execute("""
                UPDATE acquisition_watchlist
                SET status = 'conditional_set', entry_conditions = ?
                WHERE ticker = ? AND status IN ('researched', 'pending')
            """, (thesis_text[:500], ticker))
            conn.commit()

        except Exception as e:
            logger.error(f"  ❌ conditional_tracking write failed for {ticker}: {e}")

    else:
        reason = (
            f"confidence {confidence:.2f} < threshold {CONFIDENCE_THRESHOLD}"
            if not should_enter or confidence < CONFIDENCE_THRESHOLD
            else "analyst_pass"
        )
        logger.info(f"  ⏭️  {ticker} skipped: {reason}")

        # Mark library entry as analyst-reviewed but below threshold
        conn.execute("""
            UPDATE stock_research_library SET status = 'analyst_pass'
            WHERE ticker = ? AND status IN ('library_ready','partial')
        """, (ticker,))
        
        # Build descriptive pass text for the thesis column
        thesis      = result.get('thesis_summary', '') or '' if result else ''
        gaps        = (result.get('data_gaps', []) or []) if result else []
        risks       = (result.get('key_risks', []) or []) if result else []
        re_entry    = '; '.join(str(g) for g in gaps[:2]) if gaps else 'insufficient data / low confidence'
        risk_str    = ', '.join(str(r) for r in risks[:2]) if risks else ''
        pass_text   = f"PASS ({confidence:.0%} conf)"
        if thesis:    pass_text += f" — {thesis}"
        pass_text  += f" ◆ Re-entry if: {re_entry}"
        if risk_str:  pass_text += f" ◆ Risks: {risk_str}"

        # Move to analyst_pass — write reasoning to thesis column, keep numeric ratio in notes
        conn.execute("""
            UPDATE acquisition_watchlist
            SET status = 'analyst_pass',
                entry_conditions = ?,
                notes = ?,
                created_at = CURRENT_TIMESTAMP
            WHERE ticker = ? AND status IN ('researched', 'pending')
        """, (pass_text[:500], reason, ticker))
        conn.commit()

    # Always update library status to 'analysed' (unless already set to analyst_pass/error above)
    conn.execute("""
        UPDATE stock_research_library SET status = 'analysed'
        WHERE ticker = ? AND status IN ('library_ready','partial')
    """, (ticker,))
    conn.commit()

    return result


# ── Pipeline entry point ───────────────────────────────────────────────────────

def run_analyst_cycle() -> List[Dict]:
    """
    Main pipeline function called by orchestrator.

    1. Fetch all 'library_ready' or 'partial' rows from stock_research_library
    2. For each: run Gemini 3 Pro analysis
    3. Write conditionals to conditional_tracking if above threshold
    4. Return list of analysis results

    Returns list of result dicts (one per ticker processed).
    """
    date_str = datetime.now().strftime('%Y-%m-%d')
    logger.info(f"🧠 Acquisition Analyst: starting cycle for {date_str}")

    conn = _get_conn()
    _ensure_conditional_table(conn)

    # Fetch tickers ready for analysis
    try:
        cursor = conn.execute("""
            SELECT *
            FROM stock_research_library
            WHERE status IN ('library_ready', 'partial')
            ORDER BY created_at ASC
            LIMIT 5
        """)
        ready = [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Failed to fetch ready research: {e}")
        conn.close()
        return []

    if not ready:
        logger.info("  📭 No research ready for analysis")
        conn.close()
        return []

    tickers = [r['ticker'] for r in ready]
    logger.info(f"  📋 {len(ready)} tickers ready for analysis: {tickers}")

    results = []
    for i, research in enumerate(ready):
        ticker = research['ticker']
        result = analyze_ticker(ticker, research, conn)
        if result:
            results.append(result)
        # Brief pause between calls — stay within 60-120 RPM limit
        if i < len(ready) - 1:
            import time as _t; _t.sleep(2)

    conn.close()
    logger.info(f"✅ Analyst cycle complete: {len(results)}/{len(ready)} analyzed")
    return results


if __name__ == '__main__':
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )

    if '--ticker' in sys.argv:
        idx = sys.argv.index('--ticker')
        ticker = sys.argv[idx + 1].upper()
        print(f"\n🧠 Single-ticker analysis: {ticker}")
        conn = _get_conn()
        _ensure_conditional_table(conn)

        # Pull research from library
        cursor = conn.execute("""
            SELECT * FROM stock_research_library
            WHERE ticker = ? ORDER BY created_at DESC LIMIT 1
        """, (ticker,))
        row = cursor.fetchone()
        if not row:
            print(f"❌ No research found for {ticker}. Run: python3 acquisition_researcher.py --ticker {ticker}")
            conn.close()
            sys.exit(1)

        result = analyze_ticker(ticker, dict(row), conn)
        conn.close()

        if result:
            print(f"\n{'='*60}")
            print(f"ANALYSIS RESULT — {ticker}")
            print(f"{'='*60}")
            print(f"  Should enter:   {result.get('should_enter')}")
            print(f"  Confidence:     {result.get('research_confidence', 0):.2f}")
            print(f"  Entry target:   ${result.get('entry_price_target', 'N/A')}")
            print(f"  Stop loss:      ${result.get('stop_loss', 'N/A')}")
            print(f"  Take profit 1:  ${result.get('take_profit_1', 'N/A')}")
            print(f"  Take profit 2:  ${result.get('take_profit_2', 'N/A')}")
            print(f"  Position size:  {result.get('position_size_pct', 0)*100:.0f}% of cash")
            print(f"  Time horizon:   {result.get('time_horizon_days', 'N/A')} days")
            print(f"  Thesis:         {result.get('thesis_summary', 'N/A')}")
            print(f"  Tokens:         {result.get('_input_tokens',0)}→{result.get('_output_tokens',0)}")
    else:
        print(f"\n🧠 Acquisition Analyst — full cycle")
        results = run_analyst_cycle()
        print(f"\nAnalyzed: {[r.get('_ticker') for r in results]}")
        promoted = [r for r in results if r.get('should_enter') and r.get('research_confidence', 0) >= CONFIDENCE_THRESHOLD]
        print(f"Promoted to broker: {[r.get('_ticker') for r in promoted]}")
