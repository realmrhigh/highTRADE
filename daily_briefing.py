#!/usr/bin/env python3
"""
HighTrade Daily Briefing
Runs once per day (configurable time, default ~4:30 PM after market close).
Synthesizes ALL data accumulated during the trading day and produces a
structured market intelligence report stored in the DB and posted to Slack.

Three-tier model framework (all use thinkingConfig.thinkingBudget):
  - fast      : gemini-2.5-flash, thinking=0   → cheap pattern check, no reasoning overhead
  - balanced  : gemini-2.5-flash, thinking=8k  → reasons before answering, same model cheaper
  - reasoning : gemini-3-pro-preview, thinking=-1 → dynamic chain-of-thought, deepest synthesis

The goal: compare output quality across thinking budgets.
Gemini 3 Pro with dynamic thinking is the primary signal source once calibrated.
"""

import json
import logging
import sqlite3
import requests
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import gemini_client

logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent.resolve()
DB_PATH = SCRIPT_DIR / 'trading_data' / 'trading_history.db'

# ── Model tiers ────────────────────────────────────────────────────────────────
# Each tier uses thinkingConfig.thinkingBudget to control depth of reasoning.
# Thinking tokens are consumed BEFORE output tokens and are NOT billed the same.
# Budget = -1 means the model decides how much to think (dynamic, best quality).
# Budget = 0 disables thinking entirely (fastest, cheapest).
#
#  Tier        Model                  Thinking  Purpose
#  ──────────  ─────────────────────  ────────  ──────────────────────────────
#  fast        gemini-2.5-flash            0    Pattern check, low cost
#  balanced    gemini-2.5-flash        8,000    Reasons before answering
#  reasoning   gemini-3-pro-preview       -1    Full chain-of-thought, best signal
# ──────────────────────────────────────────────────────────────────────────────
MODEL_CONFIG = {
    'fast': {
        'model_id':        'gemini-2.5-flash',
        'thinking_budget': 0,       # no thinking — fast & cheap
        'max_output_tokens': 8192,
        'temperature':     0.4,
        'label':           '⚡ Flash 2.5 (Fast)',
    },
    'balanced': {
        'model_id':        'gemini-2.5-flash',
        'thinking_budget': 8000,    # moderate reasoning before output
        'max_output_tokens': 8192,
        'temperature':     0.7,     # must be 1.0 when thinking is on per API spec
        'label':           '🧠 Flash 2.5 (Thinking)',
    },
    'reasoning': {
        'model_id':        'gemini-3-pro-preview',
        'thinking_budget': -1,      # dynamic — model decides depth (highest quality)
        'max_output_tokens': 16384,
        'temperature':     1.0,     # required by API when thinking is enabled
        'label':           '🔬 Gemini 3 Pro (Deep Reason)',
    },
}

# Keep MODELS as a simple id map for callers that just need the model name
MODELS = {k: v['model_id'] for k, v in MODEL_CONFIG.items()}


def _call_gemini(model_key: str, prompt: str) -> Tuple[Optional[str], int, int]:
    """
    Call Gemini via gemini_client (OAuth CLI → REST API fallback).
    Logs which auth path was used so we can track subscription usage.
    """
    cli_ok, _ = gemini_client._get_cli_status()
    auth_path = "OAuth/CLI" if cli_ok else "REST/API-key"
    cfg = MODEL_CONFIG[model_key]

    logger.info(f"  📡 {model_key} via {auth_path} ({cfg['model_id']}, thinking={cfg['thinking_budget']})")

    text, in_tok, out_tok = gemini_client.call(
        prompt=prompt,
        model_key=model_key,
    )

    if text:
        logger.info(f"  📊 {model_key} tokens: in={in_tok} out={out_tok} | auth={auth_path}")
    return text, in_tok, out_tok


def _gather_daily_context(db_path: str, date_str: str = None) -> Dict:
    """
    Pull all data accumulated for the given date from every table.
    Returns a rich context dict ready to feed to the LLM.
    """
    if not date_str:
        date_str = datetime.now().strftime('%Y-%m-%d')

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    ctx = {'date': date_str}

    # ── 1. News signal summary ──────────────────────────────────────────
    cursor.execute("""
        SELECT COUNT(*) as cycles,
               ROUND(AVG(news_score), 1) as avg_score,
               ROUND(MAX(news_score), 1) as peak_score,
               ROUND(MIN(news_score), 1) as low_score,
               SUM(breaking_count) as total_breaking,
               SUM(article_count) as total_articles,
               MAX(dominant_crisis_type) as dominant_crisis
        FROM news_signals
        WHERE DATE(timestamp) = ?
    """, (date_str,))
    row = cursor.fetchone()
    ctx['news_summary'] = dict(row) if row else {}

    # ── 2. Top scoring news signals with component breakdown ────────────
    cursor.execute("""
        SELECT timestamp, news_score, dominant_crisis_type, sentiment_summary,
               score_components_json, keyword_hits_json, breaking_count
        FROM news_signals
        WHERE DATE(timestamp) = ?
        ORDER BY news_score DESC
        LIMIT 5
    """, (date_str,))
    top_signals = []
    for r in cursor.fetchall():
        d = dict(r)
        try: d['score_components'] = json.loads(d.pop('score_components_json') or '{}')
        except: d['score_components'] = {}
        try: d['keyword_hits'] = json.loads(d.pop('keyword_hits_json') or '{}')
        except: d['keyword_hits'] = {}
        top_signals.append(d)
    ctx['top_news_signals'] = top_signals

    # ── 3. Gemini Flash summaries from today ────────────────────────────
    cursor.execute("""
        SELECT timestamp, gemini_flash_json
        FROM news_signals
        WHERE DATE(timestamp) = ? AND gemini_flash_json IS NOT NULL
        ORDER BY news_score DESC
        LIMIT 8
    """, (date_str,))
    flash_analyses = []
    for r in cursor.fetchall():
        try:
            d = json.loads(r['gemini_flash_json'])
            d['timestamp'] = r['timestamp']
            flash_analyses.append(d)
        except: pass
    ctx['flash_analyses'] = flash_analyses

    # ── 4. Gemini Pro analyses from today ───────────────────────────────
    cursor.execute("""
        SELECT model_used, trigger_type, recommended_action,
               narrative_coherence, confidence_in_signal,
               hidden_risks, contrarian_signals, reasoning, created_at
        FROM gemini_analysis
        WHERE DATE(created_at) = ?
        ORDER BY created_at DESC
        LIMIT 10
    """, (date_str,))
    ctx['pro_analyses'] = [dict(r) for r in cursor.fetchall()]

    # ── 5. DEFCON and signal score history ──────────────────────────────
    cursor.execute("""
        SELECT monitoring_time, defcon_level, signal_score,
               bond_10yr_yield, vix_close
        FROM signal_monitoring
        WHERE monitoring_date = ?
        ORDER BY monitoring_time ASC
    """, (date_str,))
    ctx['defcon_history'] = [dict(r) for r in cursor.fetchall()]

    # ── 6. Latest FRED macro reading ────────────────────────────────────
    cursor.execute("""
        SELECT yield_curve_spread, fed_funds_rate, unemployment_rate,
               m2_yoy_change, hy_oas_bps, consumer_sentiment,
               rate_10y, rate_2y, macro_score, defcon_modifier, signals_json
        FROM macro_indicators
        ORDER BY created_at DESC LIMIT 1
    """)
    row = cursor.fetchone()
    if row:
        d = dict(row)
        try: d['signals'] = json.loads(d.pop('signals_json') or '[]')
        except: d['signals'] = []
        ctx['macro'] = d
    else:
        ctx['macro'] = {}

    # ── 7. Congressional trading (last 30 days) ─────────────────────────
    cursor.execute("""
        SELECT ticker, buy_count, politicians_json, bipartisan,
               committee_relevance, signal_strength, created_at
        FROM congressional_cluster_signals
        ORDER BY signal_strength DESC, created_at DESC LIMIT 5
    """)
    clusters = []
    for r in cursor.fetchall():
        d = dict(r)
        try: d['politicians'] = json.loads(d.pop('politicians_json') or '[]')
        except: d['politicians'] = []
        try: d['committee_relevance'] = json.loads(d.get('committee_relevance') or '[]')
        except: pass
        clusters.append(d)
    ctx['congressional_clusters'] = clusters

    # ── 8. Open paper positions ─────────────────────────────────────────
    cursor.execute("""
        SELECT asset_symbol, entry_date, entry_price, shares,
               position_size_dollars, defcon_at_entry
        FROM trade_records WHERE status = 'open'
    """)
    ctx['open_positions'] = [dict(r) for r in cursor.fetchall()]

    # ── 9. Closed trades P&L this week ─────────────────────────────────
    week_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    cursor.execute("""
        SELECT asset_symbol, entry_date, exit_date, exit_reason,
               profit_loss_dollars, profit_loss_percent
        FROM trade_records
        WHERE status = 'closed' AND exit_date >= ?
        ORDER BY exit_date DESC
    """, (week_ago,))
    ctx['recent_closed'] = [dict(r) for r in cursor.fetchall()]

    conn.close()
    return ctx


_JSON_TEMPLATE = """{
  "market_regime": "one of: risk-on / risk-off / neutral / transitioning",
  "regime_confidence": 0.0,
  "headline_summary": "2-3 sentence summary of today's most important market story",
  "key_themes": ["theme1", "theme2", "theme3"],
  "biggest_risk_today": "specific risk factor with evidence from data",
  "biggest_opportunity_today": "specific opportunity with evidence from data",
  "signal_quality_assessment": "assessment of whether today's news signals were meaningful or noise",
  "macro_alignment": "how macro data aligns with or contradicts news signals",
  "congressional_alpha": "any actionable intelligence from political trading data",
  "portfolio_assessment": "assessment of current open positions given today's data",
  "watchlist_tomorrow": ["TICKER1", "TICKER2", "TICKER3"],
  "entry_conditions_tomorrow": "specific conditions that would trigger a buy signal",
  "defcon_forecast": "expected DEFCON level tomorrow if current trends continue",
  "reasoning_chain": "step-by-step walk through how you connected the data points",
  "model_confidence": 0.0
}"""


def _build_daily_prompt(ctx: Dict) -> str:
    """Build the comprehensive daily briefing prompt."""

    date = ctx['date']
    ns = ctx.get('news_summary', {})
    macro = ctx.get('macro', {})
    defcon_hist = ctx.get('defcon_history', [])
    pro = ctx.get('pro_analyses', [])
    flash = ctx.get('flash_analyses', [])
    clusters = ctx.get('congressional_clusters', [])
    positions = ctx.get('open_positions', [])
    closed = ctx.get('recent_closed', [])
    top_signals = ctx.get('top_news_signals', [])

    # Format DEFCON timeline
    defcon_timeline = ""
    if defcon_hist:
        for d in defcon_hist:
            defcon_timeline += f"  {d.get('monitoring_time','?')} — DEFCON {d.get('defcon_level','?')}, Score {d.get('signal_score',0):.1f}, VIX {d.get('vix_close','?')}, Yield {d.get('bond_10yr_yield','?')}%\n"

    # Format Pro analysis summary
    pro_summary = ""
    if pro:
        actions = [p.get('recommended_action','?') for p in pro]
        from collections import Counter
        action_counts = Counter(actions)
        pro_summary = f"Actions recommended: {dict(action_counts)}\n"
        for p in pro[:3]:
            reasoning = (p.get('reasoning') or '')[:300]
            pro_summary += f"  [{p.get('trigger_type','?')}] {p.get('recommended_action','?')} — {reasoning}\n"

    # Format Flash themes
    flash_themes = ""
    if flash:
        themes = [f.get('dominant_theme','') for f in flash if f.get('dominant_theme')]
        actions = [f.get('recommended_action','') for f in flash if f.get('recommended_action')]
        flash_themes = f"  Themes seen: {', '.join(set(themes))}\n"
        flash_themes += f"  Actions recommended: {', '.join(set(actions))}\n"

    # Format top signals
    top_signal_text = ""
    for s in top_signals[:3]:
        comps = s.get('score_components', {})
        top_signal_text += (
            f"  Score {s.get('news_score','?')} at {s.get('timestamp','?')[:16]} — "
            f"{s.get('dominant_crisis_type','?')} | {s.get('sentiment_summary','?')}\n"
            f"    Components: sentiment={comps.get('sentiment_net',0):.0f} "
            f"concentration={comps.get('signal_concentration',0):.0f} "
            f"urgency={comps.get('urgency_premium',0):.0f}\n"
        )

    # Format keyword hits
    kw_hits = {}
    for s in top_signals:
        for kw, count in (s.get('keyword_hits') or {}).items():
            kw_hits[kw] = kw_hits.get(kw, 0) + count
    top_keywords = sorted(kw_hits.items(), key=lambda x: x[1], reverse=True)[:10]
    kw_text = ', '.join(f"{k}({v})" for k, v in top_keywords)

    # Format macro
    macro_text = ""
    if macro:
        macro_text = (
            f"  Yield Curve (10Y-2Y): {macro.get('yield_curve_spread','N/A'):+.2f}%\n"
            f"  Fed Funds Rate: {macro.get('fed_funds_rate','N/A'):.2f}%\n"
            f"  Unemployment: {macro.get('unemployment_rate','N/A'):.1f}%\n"
            f"  HY Credit Spreads: {macro.get('hy_oas_bps','N/A'):.0f}bps\n"
            f"  Consumer Sentiment: {macro.get('consumer_sentiment','N/A'):.1f}\n"
            f"  Composite Macro Score: {macro.get('macro_score',50):.0f}/100\n"
        ) if isinstance(macro.get('yield_curve_spread'), float) else "  FRED data not yet available\n"

    # Format congressional
    cong_text = ""
    if clusters:
        for c in clusters[:3]:
            cong_text += (
                f"  ${c.get('ticker','?')}: {c.get('buy_count',0)} politicians, "
                f"strength={c.get('signal_strength',0):.0f}, "
                f"bipartisan={'Yes' if c.get('bipartisan') else 'No'}, "
                f"committees={c.get('committee_relevance','[]')}\n"
            )
    else:
        cong_text = "  No significant cluster signals detected today\n"

    # Format positions
    pos_text = ""
    if positions:
        for p in positions:
            pos_text += f"  {p['asset_symbol']}: {p['shares']} shares @ ${p['entry_price']:.2f} (${p['position_size_dollars']:,.0f}) — entered {p['entry_date']}\n"
    else:
        pos_text = "  No open positions (cash deployed: $0)\n"

    # Format recent trades
    trades_text = ""
    if closed:
        for t in closed:
            pnl = t.get('profit_loss_dollars', 0) or 0
            pct = t.get('profit_loss_percent', 0) or 0
            trades_text += f"  {t['asset_symbol']} exited {t['exit_date']} via {t['exit_reason']}: ${pnl:+,.2f} ({pct:+.1f}%)\n"
    else:
        trades_text = "  No closed trades this week\n"

    body = (
        "You are HighTrade's senior market strategist AI. Today is " + date + ".\n"
        "You have access to a full day of automated market monitoring data. "
        "Your job is to produce a comprehensive, actionable daily market briefing.\n\n"
        "═══════════════════════════════════════════════════════════\n"
        "SECTION 1: NEWS INTELLIGENCE (" + str(ns.get('cycles', 0)) + " monitoring cycles today)\n"
        "═══════════════════════════════════════════════════════════\n"
        "Average news score: " + str(ns.get('avg_score', 0)) + "/100"
        " (range: " + str(ns.get('low_score', 0)) + "–" + str(ns.get('peak_score', 0)) + ")\n"
        "Total articles processed: " + str(ns.get('total_articles', 0)) + "\n"
        "Breaking news events: " + str(ns.get('total_breaking', 0)) + "\n"
        "Dominant crisis type: " + str(ns.get('dominant_crisis', 'N/A')) + "\n\n"
        "Top scoring signals today:\n" + top_signal_text +
        "Most frequent financial keywords: " + kw_text + "\n\n"
        "═══════════════════════════════════════════════════════════\n"
        "SECTION 2: AI ANALYSIS CONSENSUS (Gemini Flash + Pro)\n"
        "═══════════════════════════════════════════════════════════\n"
        "Gemini Pro analysis consensus:\n" + pro_summary +
        "Gemini Flash themes across cycles:\n" + flash_themes + "\n"
        "═══════════════════════════════════════════════════════════\n"
        "SECTION 3: DEFCON & MARKET DATA TIMELINE\n"
        "═══════════════════════════════════════════════════════════\n"
        + (defcon_timeline if defcon_timeline else "  No monitoring data recorded today\n") + "\n"
        "═══════════════════════════════════════════════════════════\n"
        "SECTION 4: MACROECONOMIC ENVIRONMENT (FRED)\n"
        "═══════════════════════════════════════════════════════════\n"
        + macro_text + "\n"
        "═══════════════════════════════════════════════════════════\n"
        "SECTION 5: CONGRESSIONAL TRADING SIGNALS\n"
        "═══════════════════════════════════════════════════════════\n"
        + cong_text + "\n"
        "═══════════════════════════════════════════════════════════\n"
        "SECTION 6: PORTFOLIO STATUS\n"
        "═══════════════════════════════════════════════════════════\n"
        "Open positions:\n" + pos_text +
        "Recent closed trades:\n" + trades_text + "\n"
        "═══════════════════════════════════════════════════════════\n"
        "YOUR TASK\n"
        "═══════════════════════════════════════════════════════════\n"
        "Synthesize ALL of the above into a structured daily briefing. "
        "Be direct and specific — no hedging, no disclaimers. "
        "This is a paper trading system for learning purposes.\n\n"
        "IMPORTANT: You MUST populate every single field in the JSON. "
        "Do not leave any field as a placeholder or empty string. "
        "regime_confidence and model_confidence must be actual numbers 0.0-1.0.\n\n"
        "Respond in this exact JSON format:\n"
    )
    return body + _JSON_TEMPLATE


def run_daily_briefing(compare_models: bool = False) -> Dict:
    """
    Main entry point. Gathers today's data, runs model(s), saves to DB.

    Production mode (default):
      Runs only the 'reasoning' tier (gemini-3-pro-preview, dynamic thinking).
      This is what fires every day at market close — one deep synthesis, stored.

    Compare mode (--compare flag):
      Runs all three tiers side-by-side so you can evaluate output quality.
      Use this occasionally to validate that reasoning is still the best tier.

    Returns dict keyed by model_key with parsed briefing results.
    """
    date_str = datetime.now().strftime('%Y-%m-%d')
    mode_label = "COMPARE (all tiers)" if compare_models else "PRODUCTION (reasoning only)"
    logger.info(f"📋 Daily Briefing [{mode_label}]: gathering data for {date_str}...")

    ctx = _gather_daily_context(str(DB_PATH), date_str)

    cycles = ctx.get('news_summary', {}).get('cycles', 0)
    articles = ctx.get('news_summary', {}).get('total_articles', 0)
    pro_count = len(ctx.get('pro_analyses', []))
    logger.info(f"  📊 Context: {cycles} cycles, {articles} articles, {pro_count} Pro analyses")

    prompt = _build_daily_prompt(ctx)
    logger.info(f"  📝 Prompt built ({len(prompt)} chars)")

    # Production: only the deep reasoning model
    # Compare: all tiers for side-by-side evaluation
    models_to_run = MODEL_CONFIG if compare_models else {'reasoning': MODEL_CONFIG['reasoning']}
    results = {}

    for model_key, cfg in models_to_run.items():
        logger.info(f"  🤖 Running {model_key} ({cfg['model_id']}, thinking={cfg['thinking_budget']})...")
        try:
            text, in_tok, out_tok = _call_gemini(model_key, prompt)

            if not text:
                logger.warning(f"  ⚠️  {model_key} returned no response")
                results[model_key] = {'error': 'No response', 'model': cfg['model_id']}
                continue

            # Parse JSON
            parsed = _parse_briefing_response(text)
            parsed['_model'] = cfg['model_id']
            parsed['_model_key'] = model_key
            parsed['_input_tokens'] = in_tok
            parsed['_output_tokens'] = out_tok
            parsed['_raw'] = text

            logger.info(
                f"  ✅ {model_key}: regime={parsed.get('market_regime','?')}, "
                f"confidence={parsed.get('model_confidence',0):.2f} "
                f"({in_tok}→{out_tok} tokens)"
            )
            results[model_key] = parsed

        except Exception as e:
            logger.error(f"  ❌ {model_key} failed: {e}")
            results[model_key] = {'error': str(e), 'model': cfg['model_id']}

    # Save all results to DB
    _save_to_db(date_str, ctx, results)

    # Send Slack summary
    _send_slack_summary(date_str, ctx, results)

    return results


def _parse_briefing_response(text: str) -> Dict:
    """Parse JSON from model response, with fallback to raw text extraction."""
    # Strip markdown fences
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()

    # For thinking model: strip <think> blocks if present
    if "<think>" in text:
        # Extract just the final JSON after thinking block
        parts = text.split("</think>")
        if len(parts) > 1:
            text = parts[-1].strip()

    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        # Attempt repair
        for end in range(len(text), 0, -1):
            candidate = text[:end]
            opens = candidate.count('{') - candidate.count('}')
            if opens > 0:
                repaired = candidate.rstrip(',\n ') + ('}' * opens)
                try:
                    return json.loads(repaired)
                except json.JSONDecodeError:
                    continue

        # Last resort: return raw text in a structured wrapper
        return {
            'market_regime': 'unknown',
            'model_confidence': 0.0,
            'headline_summary': text[:500],
            '_parse_failed': True
        }


def _save_to_db(date_str: str, ctx: Dict, results: Dict):
    """Save daily briefing results to database."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    cursor = conn.cursor()

    # Ensure table exists
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_briefings (
            briefing_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT NOT NULL,
            model_key       TEXT NOT NULL,
            model_id        TEXT,
            market_regime   TEXT,
            regime_confidence REAL,
            headline_summary TEXT,
            key_themes_json TEXT,
            biggest_risk    TEXT,
            biggest_opportunity TEXT,
            signal_quality  TEXT,
            macro_alignment TEXT,
            congressional_alpha TEXT,
            portfolio_assessment TEXT,
            watchlist_json  TEXT,
            entry_conditions TEXT,
            defcon_forecast TEXT,
            reasoning_chain TEXT,
            model_confidence REAL,
            input_tokens    INTEGER,
            output_tokens   INTEGER,
            full_response_json TEXT,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(date, model_key)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_briefing_date ON daily_briefings(date)")

    for model_key, result in results.items():
        if 'error' in result and len(result) <= 2:
            continue
        try:
            cursor.execute("""
                INSERT OR REPLACE INTO daily_briefings
                (date, model_key, model_id, market_regime, regime_confidence,
                 headline_summary, key_themes_json, biggest_risk, biggest_opportunity,
                 signal_quality, macro_alignment, congressional_alpha,
                 portfolio_assessment, watchlist_json, entry_conditions,
                 defcon_forecast, reasoning_chain, model_confidence,
                 input_tokens, output_tokens, full_response_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                date_str,
                model_key,
                result.get('_model', ''),
                result.get('market_regime', ''),
                result.get('regime_confidence', 0),
                result.get('headline_summary', ''),
                json.dumps(result.get('key_themes', [])),
                result.get('biggest_risk_today', ''),
                result.get('biggest_opportunity_today', ''),
                result.get('signal_quality_assessment', ''),
                result.get('macro_alignment', ''),
                result.get('congressional_alpha', ''),
                result.get('portfolio_assessment', ''),
                json.dumps(result.get('watchlist_tomorrow', [])),
                result.get('entry_conditions_tomorrow', ''),
                result.get('defcon_forecast', ''),
                result.get('reasoning_chain', ''),
                result.get('model_confidence', 0),
                result.get('_input_tokens', 0),
                result.get('_output_tokens', 0),
                json.dumps({k: v for k, v in result.items() if not k.startswith('_')})
            ))
        except Exception as e:
            logger.warning(f"DB save failed for {model_key}: {e}")

    conn.commit()
    conn.close()
    logger.info(f"  💾 Daily briefings saved to DB for {len(results)} models")

    # Push watchlist tickers to acquisition queue (reasoning tier is authoritative)
    _queue_acquisition_watchlist(date_str, results)

    # Reverify all active acquisition conditionals with Flash
    _run_acquisition_verification()


def _queue_acquisition_watchlist(date_str: str, results: Dict):
    """
    After the daily briefing, push the reasoning tier's watchlist tickers into
    the acquisition_watchlist table for the (future) acquisition team to research.
    Each ticker gets a row with the briefing date, source reasoning, and status='pending'.
    """
    # Use reasoning tier as the authoritative source; fall back to any available result
    result = results.get('reasoning') or results.get('balanced') or results.get('fast') or {}
    tickers = result.get('watchlist_tomorrow', [])
    if not tickers:
        return

    entry_conditions = result.get('entry_conditions_tomorrow', '')
    market_regime    = result.get('market_regime', '')
    confidence       = result.get('model_confidence', 0)
    risk             = result.get('biggest_risk_today', '')
    opportunity      = result.get('biggest_opportunity_today', '')

    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS acquisition_watchlist (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                date_added          TEXT NOT NULL,
                ticker              TEXT NOT NULL,
                source              TEXT DEFAULT 'daily_briefing',
                market_regime       TEXT,
                model_confidence    REAL,
                entry_conditions    TEXT,
                biggest_risk        TEXT,
                biggest_opportunity TEXT,
                status              TEXT DEFAULT 'pending',
                notes               TEXT,
                created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(date_added, ticker)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_acq_date ON acquisition_watchlist(date_added)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_acq_status ON acquisition_watchlist(status)")

        for ticker in tickers:
            if not ticker or not isinstance(ticker, str):
                continue
            conn.execute("""
                INSERT OR REPLACE INTO acquisition_watchlist
                (date_added, ticker, source, market_regime, model_confidence,
                 entry_conditions, biggest_risk, biggest_opportunity, status)
                VALUES (?, ?, 'daily_briefing', ?, ?, ?, ?, ?, 'pending')
            """, (date_str, ticker.upper().strip(), market_regime, confidence,
                  entry_conditions, risk, opportunity))

        conn.commit()
        conn.close()
        logger.info(f"  📥 Acquisition queue: {len(tickers)} tickers added for {date_str} → {tickers}")

    except Exception as e:
        logger.warning(f"Acquisition watchlist queue failed: {e}")


def _run_acquisition_verification():
    """
    Run Flash reverification of all active acquisition conditionals.
    Called automatically at the end of every daily briefing.
    """
    try:
        import acquisition_verifier
        summary = acquisition_verifier.run_verification_cycle()
        logger.info(
            f"  🔍 Verifier: confirmed={summary.get('confirmed',0)}, "
            f"flagged={summary.get('flagged',0)}, "
            f"invalidated={summary.get('invalidated',0)}"
        )
    except Exception as e:
        logger.warning(f"Acquisition verification failed: {e}")


def _send_slack_summary(date_str: str, ctx: Dict, results: Dict):
    """Post daily briefing to #logs-silent. Production: reasoning tier only. Compare: all tiers."""
    try:
        from alerts import AlertSystem
        alerts = AlertSystem()

        ns  = ctx.get('news_summary', {})
        macro = ctx.get('macro', {})

        # ── Header ──────────────────────────────────────────────────────────
        text = (
            f"📋 *Daily Market Briefing — {date_str}*\n"
            f"{'='*50}\n"
            f"📊 {ns.get('cycles',0)} monitoring cycles | {ns.get('total_articles',0)} articles "
            f"| Peak score: {ns.get('peak_score',0)}/100 | Breaking events: {ns.get('total_breaking',0)}\n"
        )
        if isinstance(macro.get('macro_score'), (int, float)):
            text += (
                f"🏦 Macro: {macro.get('macro_score',50):.0f}/100 "
                f"| YieldCurve: {macro.get('yield_curve_spread',0):+.2f}% "
                f"| HY: {macro.get('hy_oas_bps',0):.0f}bps\n"
            )

        # ── Determine which tiers to show ───────────────────────────────────
        # In compare mode all keys are present; in production only 'reasoning' exists
        tier_keys = [k for k in ['fast', 'balanced', 'reasoning'] if k in results]

        for model_key in tier_keys:
            result = results.get(model_key, {})
            label  = MODEL_CONFIG.get(model_key, {}).get('label', model_key)

            if not result or ('error' in result and len(result) <= 2):
                # Only print failures when running a comparison test
                if len(tier_keys) > 1:
                    text += f"\n{label}: ❌ {result.get('error','Failed')}\n"
                continue

            regime       = result.get('market_regime', '?')
            confidence   = result.get('model_confidence', 0)
            regime_emoji = '🟢' if regime == 'risk-on' else '🔴' if regime == 'risk-off' else '🟡'
            in_tok       = result.get('_input_tokens', 0)
            out_tok      = result.get('_output_tokens', 0)

            # Full sentences — no truncation mid-thought
            headline = (result.get('headline_summary') or '').strip()
            risk     = (result.get('biggest_risk_today') or '').strip()
            opp      = (result.get('biggest_opportunity_today') or '').strip()
            entry    = (result.get('entry_conditions_tomorrow') or '').strip()
            watchlist = result.get('watchlist_tomorrow', [])
            themes   = result.get('key_themes', [])
            defcon   = result.get('defcon_forecast', '?')

            text += (
                f"\n{'─'*50}\n"
                f"🔬 *Gemini 3 Pro Analysis* | {regime_emoji} {regime.upper()} "
                f"| conf={confidence:.2f} | {in_tok}→{out_tok}tok\n\n"
                f"📰 *Summary:* {headline}\n\n"
                f"🔑 *Key Themes:* {' | '.join(themes) if themes else 'N/A'}\n\n"
                f"⚠️  *Biggest Risk:* {risk}\n\n"
                f"💡 *Biggest Opportunity:* {opp}\n\n"
                f"📅 *Entry Conditions Tomorrow:* {entry}\n\n"
                f"🔭 *DEFCON Forecast:* {defcon}\n\n"
                f"👀 *Watchlist:* {', '.join(watchlist) if watchlist else 'None'}\n"
            )

        webhook_url = alerts.config.get('channels', {}).get('slack_logging', {}).get('webhook_url')
        if webhook_url:
            payload = {
                'text': text,
                'username': 'HighTrade Daily Briefing',
                'icon_emoji': ':bar_chart:'
            }
            requests.post(webhook_url, json=payload, timeout=10)
            logger.info("  📤 Daily briefing posted to #logs-silent")

    except Exception as e:
        logger.warning(f"Slack daily briefing failed: {e}")


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    import sys
    compare_mode = '--compare' in sys.argv
    date_override = next((a for a in sys.argv[1:] if a.startswith('20')), None)

    print(f"\n📋 HighTrade Daily Briefing\n{'='*60}")
    print(f"  Date: {date_override or datetime.now().strftime('%Y-%m-%d')}")
    if compare_mode:
        print(f"  Mode: COMPARE — Fast (no-think) | Balanced (thinking=8k) | Reasoning (Gemini 3 Pro, dynamic)")
    else:
        print(f"  Mode: PRODUCTION — Gemini 3 Pro (deep reasoning, dynamic thinking)")
    print()

    results = run_daily_briefing(compare_models=compare_mode)

    print(f"\n{'='*60}")
    print(f"RESULTS")
    print(f"{'='*60}")

    print_keys = ['fast', 'balanced', 'reasoning'] if compare_mode else ['reasoning']

    for model_key in print_keys:
        r = results.get(model_key, {})
        cfg = MODEL_CONFIG.get(model_key, {})
        if 'error' in r and len(r) <= 2:
            print(f"\n❌ {model_key.upper()}: {r.get('error')}")
            continue

        print(f"\n{'─'*40}")
        print(f"🤖 {cfg.get('label', model_key)} ({r.get('_model','?')}, thinking={cfg.get('thinking_budget','?')})")
        print(f"   Tokens: {r.get('_input_tokens',0)}→{r.get('_output_tokens',0)}")
        print(f"   Regime: {r.get('market_regime','?')} (confidence: {r.get('model_confidence',0):.2f})")
        print(f"   Headline: {(r.get('headline_summary') or '')[:200]}")
        print(f"   Risk: {(r.get('biggest_risk_today') or '')[:150]}")
        print(f"   Opportunity: {(r.get('biggest_opportunity_today') or '')[:150]}")
        print(f"   Watchlist: {r.get('watchlist_tomorrow', [])}")
        print(f"   DEFCON tomorrow: {r.get('defcon_forecast','?')}")
        if r.get('reasoning_chain'):
            print(f"   Reasoning: {r['reasoning_chain'][:400]}...")
