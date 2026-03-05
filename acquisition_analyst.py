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
CONFIDENCE_THRESHOLD       = 0.70   # minimum to promote to broker (standard pipeline)
HOUND_CONFIDENCE_THRESHOLD = 0.60   # lower bar for Grok Hound speculative picks
MAX_POSITION_PCT           = 0.20   # hard cap: max 20% of capital in any single trade


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
  "data_gaps": ["<specific missing data that would improve entry precision or confidence — e.g. 'options open interest at $460 strike', 'insider buying last 30 days', 'next earnings date not in research'>"],
  "catalyst_event": "<brief description of the specific event driving this trade, e.g. 'Robotaxi product launch 2026-03-01', or null if not event-driven>",
  "catalyst_window_hours": null,
  "catalyst_spike_pct": null,
  "catalyst_failure_pct": null
}"""

# Maximum stop loss % per watch tag (hard ceiling enforced in code + prompt)
STOP_MAX_PCT = {
    'breakout':       0.07,   #  7% — tight; if it breaks out, it shouldn't retreat
    'mean-reversion': 0.08,   #  8% — a bit more room for support to hold
    'momentum':       0.07,   #  7% — trend should stay intact, no deep retrace
    'defensive-hedge':0.06,   #  6% — insurance, not a gamble; small size, tight stop
    'macro-hedge':    0.06,   #  6% — hedges are directional bets; control the loss
    'earnings-play':  0.10,   # 10% — some event volatility tolerance, still bounded
    'rebound':        0.06,   #  6% — re-entry on a broken name; tight by design
}
_DEFAULT_STOP_MAX_PCT = 0.08  # fallback for unknown tags

# Watch tag definitions injected into every analyst prompt
_WATCH_TAG_DEFINITIONS = """
══════════════════════════════════════════════════════
WATCH TAGS — assign exactly ONE to this trade
══════════════════════════════════════════════════════
Choose the tag that best describes the SETUP TYPE. It shapes your entry, sizing, and conditions.
STOP LOSS HARD CAPS (enforced by the trading system — do NOT exceed these):
  breakout        max  7% below entry
  mean-reversion  max  8% below entry
  momentum        max  7% below entry
  defensive-hedge max  6% below entry
  macro-hedge     max  6% below entry
  earnings-play   max 10% below entry   ← for high-vol events only; tighter is better
  rebound         max  6% below entry
If you cannot find a technically meaningful stop within these bands, REDUCE position size instead
of widening the stop. The system default stop is -3% — your stop should be tighter or match it,
never significantly wider. High-volatility names require SMALLER SIZE, not a bigger stop cushion.

  breakout       — Price testing or clearing a key resistance level (52w high, prior pivot).
                   Entry: above resistance. Stop: ≤7% below entry, just under the breakout base.
                   Conditions: volume + momentum confirmation.

  mean-reversion — Overextended pullback to known support; expecting bounce back to mean.
                   Entry: at support. Stop: ≤8% below entry, just below that support level.
                   Conditions: oversold signal, no trend breakdown.

  momentum       — Strong established trend; adding on a healthy pullback.
                   Entry: near moving average or recent base. Stop: ≤7% below entry, momentum-based.
                   Conditions: trend intact.

  defensive-hedge — Risk-off asset (TLT, GLD, utilities) held during macro uncertainty.
                   Entry: any weakness. Stop: ≤6% below entry. Size: small (portfolio insurance).
                   Conditions: macro score, VIX environment.

  macro-hedge    — Inverse or volatility instrument (SQQQ, VIX products, short ETFs).
                   Entry: strict — only when VIX > threshold AND DEFCON elevated.
                   Stop: ≤6% below entry. Size: smaller.

  earnings-play  — Setup driven by an upcoming earnings catalyst.
                   Entry: before event date. Stop: ≤10% below entry (catalyst failure level).
                   Time horizon: short (exit after announcement or catalyst window).

  rebound        — Post-stop-loss recovery attempt on a previously held ticker.
                   Entry: on bottoming signal. Stop: ≤6% below entry. Size: reduced (half of normal).
                   Conditions: must see exhaustion of selling before re-entry.
"""


def _get_hound_context(ticker: str, conn: sqlite3.Connection) -> Optional[Dict]:
    """Return Grok Hound intel if this ticker was sourced from grok_hound_auto, else None."""
    try:
        row = conn.execute("""
            SELECT source FROM acquisition_watchlist
            WHERE UPPER(ticker) = UPPER(?) AND source = 'grok_hound_auto'
            ORDER BY created_at DESC LIMIT 1
        """, (ticker,)).fetchone()
        if not row:
            return None
        hound_row = conn.execute("""
            SELECT alpha_score, why_next, signals, action_suggestion
            FROM grok_hound_candidates
            WHERE UPPER(ticker) = UPPER(?)
            ORDER BY created_at DESC LIMIT 1
        """, (ticker,)).fetchone()
        if not hound_row:
            return {'source': 'grok_hound_auto'}
        return {
            'source':            'grok_hound_auto',
            'alpha_score':       hound_row[0],
            'why_next':          hound_row[1],
            'signals':           json.loads(hound_row[2]) if hound_row[2] else [],
            'action_suggestion': hound_row[3],
        }
    except Exception as e:
        logger.debug(f"Could not load hound context for {ticker}: {e}")
        return None


def _build_analyst_prompt(ticker: str, research: Dict,
                           prior_gaps: Optional[List[str]] = None,
                           hound_context: Optional[Dict] = None) -> str:
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
    rec_key  = research.get('recommendation_key') or 'N/A'
    rec_mean = research.get('recommendation_mean')
    n_analysts = research.get('analyst_count')
    upside   = ((tgt_mean - price) / price * 100) if (isinstance(tgt_mean, float) and isinstance(price, float)) else None

    analyst_block = (
        f"  Price targets:    Mean ${tgt_mean} / High ${tgt_high} / Low ${tgt_low}\n"
        f"  Implied upside:   {f'{upside:+.1f}%' if upside is not None else 'N/A'}\n"
        f"  Ratings (recent): {buy_c} Buy / {hold_c} Hold / {sell_c} Sell\n"
        f"  Consensus:        {rec_key}"
        + (f" (mean {rec_mean:.2f}/5.0)" if isinstance(rec_mean, float) else "")
        + (f" — {n_analysts} analysts" if n_analysts else "") + "\n"
    )

    # Earnings
    next_earn    = research.get('next_earnings_date') or 'Unknown'
    eps_surprise = research.get('last_eps_surprise_pct')
    eps_str      = f"{eps_surprise:+.1f}%" if isinstance(eps_surprise, float) else 'N/A'

    earnings_block = (
        f"  Next earnings:     {next_earn}\n"
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

    # Short interest (data_bridge)
    short_pct   = research.get('short_pct_float')
    short_ratio = research.get('short_ratio')
    short_date  = research.get('short_date') or 'N/A'
    shares_short = research.get('shares_short')

    def _fmt_short_pct(v):
        return f"{v*100:.1f}%" if isinstance(v, float) else 'N/A'
    def _fmt_shares(v):
        return f"{v:,}" if isinstance(v, (int, float)) and v else 'N/A'

    short_block = (
        f"  Short % of float: {_fmt_short_pct(short_pct)}"
        + (f" ({_fmt_shares(shares_short)} shares)" if shares_short else "") + "\n"
        f"  Days to cover:    {f'{short_ratio:.1f}' if isinstance(short_ratio, float) else 'N/A'}\n"
        f"  Short data date:  {short_date}\n"
    )

    # Options snapshot (data_bridge)
    atm_iv_call = research.get('options_atm_iv_call')
    atm_iv_put  = research.get('options_atm_iv_put')
    pcr         = research.get('options_put_call_ratio')
    call_oi     = research.get('options_total_call_oi')
    put_oi      = research.get('options_total_put_oi')
    expiry      = research.get('options_nearest_expiry') or 'N/A'

    def _fmt_iv(v):
        return f"{v*100:.1f}%" if isinstance(v, float) else 'N/A'
    def _fmt_oi(v):
        return f"{v:,}" if isinstance(v, (int, float)) and v else 'N/A'

    options_block = (
        f"  Nearest expiry:   {expiry}\n"
        f"  ATM IV (call/put): {_fmt_iv(atm_iv_call)} / {_fmt_iv(atm_iv_put)}\n"
        f"  Put/Call OI ratio: {f'{pcr:.2f}' if isinstance(pcr, float) else 'N/A'}"
        + (" (>1.0 = bearish lean)" if isinstance(pcr, float) and pcr > 1.0 else
           " (<1.0 = bullish lean)" if isinstance(pcr, float) else "") + "\n"
        f"  Total OI (C/P):   {_fmt_oi(call_oi)} / {_fmt_oi(put_oi)}\n"
    )

    # Pre-market (data_bridge)
    pre_price = research.get('pre_market_price')
    pre_chg   = research.get('pre_market_chg_pct')
    vix_lvl   = research.get('vix_level')

    premarket_block = (
        f"  Pre-market price: {'${:.2f}'.format(pre_price) if pre_price else 'N/A'}"
        + (f" ({pre_chg:+.2f}%)" if isinstance(pre_chg, float) else "") + "\n"
        f"  VIX (latest):     {f'{vix_lvl:.2f}' if isinstance(vix_lvl, float) else 'N/A'}\n"
    )

    # Insider activity (data_bridge)
    ins_buys  = research.get('insider_buys_90d', 0)
    ins_sells = research.get('insider_sells_90d', 0)
    ins_sent  = research.get('insider_net_sentiment') or 'neutral'
    ins_last  = research.get('insider_last_date') or 'N/A'

    insider_block = (
        f"  Insider txns (90d): {ins_buys} buys / {ins_sells} sells → {ins_sent.upper()}\n"
        f"  Last transaction:   {ins_last}\n"
    )

    # Internal signals
    news_count   = research.get('news_mention_count', 0)
    news_sent    = research.get('news_sentiment_avg')
    news_zero    = research.get('news_zero_reason')
    cong_strength = research.get('congressional_signal_strength', 0)
    cong_buys    = research.get('congressional_buy_count', 0)
    macro_score  = research.get('macro_score', 'N/A')

    news_count_str = str(news_count)
    if news_count == 0 and news_zero:
        news_count_str = f"0 — {news_zero}"

    signals_block = (
        f"  News mentions (30d): {news_count_str}\n"
        f"  News sentiment avg:  {f'{news_sent:.1f}/100' if isinstance(news_sent, float) else 'N/A'}\n"
        f"  Congressional signal strength: {cong_strength:.0f}\n"
        f"  Congressional buy count:       {cong_buys}\n"
        f"  Macro composite score:         {macro_score}\n"
    )

    import gemini_client as _gc
    _session_block = _gc.market_context_block()

    # Build Hound intelligence block if this ticker came from Grok Hound
    _hound_block = ""
    if hound_context:
        _action_display = (hound_context.get('action_suggestion') or 'monitor').upper().replace('_', ' ')
        _alpha          = hound_context.get('alpha_score', 'N/A')
        _why_next       = hound_context.get('why_next', 'N/A')
        _sigs           = hound_context.get('signals', [])
        _hound_block = (
            f"══════════════════════════════════════════════════════\n"
            f"GROK HOUND INTELLIGENCE — SOURCE OF THIS LEAD\n"
            f"══════════════════════════════════════════════════════\n"
            f"  Action suggestion: {_action_display}\n"
            f"  Alpha score:       {_alpha}/100\n"
            f"  Hound thesis:      {_why_next}\n"
            f"  X signals:         {', '.join(_sigs) if _sigs else 'N/A'}\n\n"
            f"⚡ HOUND STRATEGY FRAME — read this before you start:\n"
            f"  This is a SHORT-TERM SPECULATIVE setup, NOT a long-term value acquisition.\n"
            f"  Do NOT over-anchor on P/E ratios, analyst price targets, or earnings dates.\n"
            f"  The edge here is the Hound's real-time signal (X velocity, short squeeze, rotation).\n"
            f"  Position size: SMALL (3–7% of cash) — asymmetric bet, not a core position.\n"
            f"  Time horizon: SHORT (5–15 days) — follow the catalyst window, then exit.\n"
            f"  Preferred watch_tag: momentum or breakout.\n"
            f"  Confidence threshold: 0.60 (lower bar applies — you are sizing small to match the risk).\n"
            f"  Only set should_enter=true if research_confidence >= 0.60.\n\n"
        )

    return (
        f"You are HighTrade's senior acquisition analyst. Today is {datetime.now().strftime('%Y-%m-%d')}.\n"
        f"You have been given comprehensive research on {ticker} gathered from multiple sources.\n"
        f"Your job: determine whether to set a CONDITIONAL ENTRY ORDER on {ticker}.\n\n"
        f"This is a paper trading system. Be precise and specific — no vague answers.\n"
        f"If you recommend entering, every price level must be a real number.\n\n"
        f"{_session_block}\n"
        f"{_hound_block}"
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
        f"SHORT INTEREST\n"
        f"══════════════════════════════════════════════════════\n"
        f"{short_block}\n"
        f"══════════════════════════════════════════════════════\n"
        f"OPTIONS MARKET SNAPSHOT\n"
        f"══════════════════════════════════════════════════════\n"
        f"{options_block}\n"
        f"══════════════════════════════════════════════════════\n"
        f"PRE-MARKET & VIX\n"
        f"══════════════════════════════════════════════════════\n"
        f"{premarket_block}\n"
        f"══════════════════════════════════════════════════════\n"
        f"INSIDER ACTIVITY (Form 4 — last 90 days)\n"
        f"══════════════════════════════════════════════════════\n"
        f"{insider_block}\n"
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
        f"  8. What would invalidate this thesis entirely?\n"
        f"  9. CATALYST CHECK: Is this trade driven by a specific upcoming event "
        f"(earnings, product launch, FDA decision, macro announcement)?\n"
        f"     If yes, set: catalyst_event (name+date), catalyst_window_hours (how long to watch),\n"
        f"     catalyst_spike_pct (sell into strength if up >= X% within window),\n"
        f"     catalyst_failure_pct (exit early if down >= X% — must be negative, tighter than stop).\n"
        f"     Reference: product reveal/sell-the-news → spike=1.5,failure=-1.5,window=24 | "
        f"sustained product launch → spike=3,failure=-2,window=48 | "
        f"earnings → spike=4,failure=-5,window=24 | FDA → spike=8,failure=-5,window=24\n"
        f"     KEY: for single-event reveals (robotaxi, product drop), spike=1.5 means:\n"
        f"     'if price hasn't moved >=1.5% within 24h, the news is a dud — exit.'\n"
        f"     VOLATILITY SCALING: these are mid-vol baselines (beta ~1.0). For high-beta/high-IV\n"
        f"     stocks (TSLA, GME — beta >1.5 or ATM IV >50%), scale spike_pct up 1.5-2x because\n"
        f"     a 1.5% move is normal noise for those names. e.g. TSLA product reveal → spike=2.5-3.0.\n"
        f"     If NOT event-driven, set all catalyst fields to null.\n\n"
        f"Set research_confidence as a float 0.0–1.0 based on how convinced you are.\n"
        f"Only set should_enter=true if research_confidence >= "
        f"{'0.60' if hound_context else f'{CONFIDENCE_THRESHOLD:.2f}'}.\n\n"
        + (
            f"📋 PREVIOUSLY IDENTIFIED DATA GAPS — from prior research passes on {ticker}:\n"
            + ''.join(f"  • {g}\n" for g in (prior_gaps or []))
            + f"These gaps caused reduced confidence in earlier analyses. "
              f"If the current research package fills any of these, explicitly note it in your reasoning. "
              f"If they remain unresolved, include them again in data_gaps.\n\n"
            if prior_gaps else ""
        )
        + f"⚠️  NUMERIC CONSISTENCY RULE — enforced before submission:\n"
        f"  The dollar value in 'stop_loss' MUST appear verbatim in 'stop_loss_rationale'.\n"
        f"  The dollar value in 'entry_price_target' MUST appear verbatim in 'entry_price_rationale'.\n"
        f"  The dollar value in 'take_profit_1' MUST appear verbatim in 'take_profit_rationale'.\n"
        f"  If your rationale text says '$85' but your JSON field says 85.5 — fix one to match.\n"
        f"  Do NOT write a different number in the text than in the JSON field. They must be identical.\n\n"
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

    # Pull any previously identified data gaps for this ticker (closed-loop feedback)
    prior_gaps: List[str] = []
    try:
        gap_row = conn.execute("""
            SELECT data_gaps_json FROM conditional_tracking
            WHERE UPPER(ticker) = UPPER(?) AND data_gaps_json IS NOT NULL
            ORDER BY updated_at DESC LIMIT 1
        """, (ticker,)).fetchone()
        if gap_row and gap_row[0]:
            raw = json.loads(gap_row[0])
            prior_gaps = [g for g in (raw or []) if isinstance(g, str) and g.strip()]
        if prior_gaps:
            logger.info(f"  🔁 Feeding {len(prior_gaps)} prior data gap(s) back into prompt for {ticker}")
    except Exception as _pge:
        logger.debug(f"  ⚠️  Could not load prior data gaps for {ticker}: {_pge}")

    # Pull Hound intel if this ticker originated from grok_hound_auto
    hound_context = _get_hound_context(ticker, conn)
    if hound_context:
        logger.info(f"  🐕 {ticker} is a Hound pick (alpha={hound_context.get('alpha_score')}, "
                    f"action={hound_context.get('action_suggestion')}) — applying Hound strategy frame")

    prompt = _build_analyst_prompt(ticker, research, prior_gaps=prior_gaps or None,
                                   hound_context=hound_context)

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
    result['_model']         = 'gemini-3.1-pro-preview'
    result['_input_tokens']  = in_tok
    result['_output_tokens'] = out_tok

    # ── Hard-cap stop loss to strategy limits ────────────────────────────
    # The system default stop is -3%.  Analyst stops must stay within the per-tag
    # cap defined in STOP_MAX_PCT.  Wide stops defeat the strategy — use smaller
    # size instead of wider stops for high-volatility names.
    _entry = result.get('entry_price_target')
    _stop  = result.get('stop_loss')
    _tag   = result.get('watch_tag', '')
    if _entry and _stop and _entry > 0:
        _actual_pct = (_stop - _entry) / _entry           # negative number
        _max_pct    = -STOP_MAX_PCT.get(_tag, _DEFAULT_STOP_MAX_PCT)
        if _actual_pct < _max_pct:                        # stop is wider than the cap
            _capped_stop = round(_entry * (1 + _max_pct), 2)
            logger.warning(
                f"  ⚠️  [{ticker}] Stop {_stop:.2f} ({_actual_pct*100:.1f}%) exceeds "
                f"{_tag or 'default'} cap ({_max_pct*100:.0f}%) — "
                f"capping to {_capped_stop:.2f}"
            )
            result['stop_loss'] = _capped_stop
            result['stop_loss_rationale'] = (
                f"[AUTO-CAPPED from ${_stop:.2f} to ${_capped_stop:.2f}] "
                f"Original stop exceeded the {abs(_max_pct*100):.0f}% strategy limit for '{_tag}' setups. "
                + (result.get('stop_loss_rationale') or '')
            )

    confidence   = float(result.get('research_confidence', 0.0))
    should_enter = result.get('should_enter', False)

    logger.info(
        f"  📊 {ticker}: should_enter={should_enter}, confidence={confidence:.2f} "
        f"({in_tok}→{out_tok} tok)"
    )

    # ── Write to conditional_tracking if above threshold ─────────────────
    _effective_threshold = HOUND_CONFIDENCE_THRESHOLD if hound_context else CONFIDENCE_THRESHOLD
    if should_enter and confidence >= _effective_threshold:
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
                    catalyst_event, catalyst_window_hours,
                    catalyst_spike_pct, catalyst_failure_pct,
                    status, last_verified
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?)
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
                result.get('catalyst_event'),
                result.get('catalyst_window_hours'),
                result.get('catalyst_spike_pct'),
                result.get('catalyst_failure_pct'),
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
            f"confidence {confidence:.2f} < threshold {_effective_threshold}"
            if not should_enter or confidence < _effective_threshold
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
        # RPM pacing is now handled automatically inside gemini_client._call_via_cli
        # via _throttle_for_rpm() — no manual sleep needed here.

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
