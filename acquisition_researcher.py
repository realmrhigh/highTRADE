#!/usr/bin/env python3
"""
acquisition_researcher.py — Data collection layer for the acquisition pipeline.

Flow:
  acquisition_watchlist (status='pending')
      ↓ [this module]
  stock_research_library (status='library_ready')
      ↓ [acquisition_analyst.py]
  conditional_tracking (status='active')
      ↓ [broker_agent.py conditional checking]
  trade_records

Data sources:
  - Unusual Whales API : price, fundamentals, analyst targets, earnings
  - SEC EDGAR : latest 10-K/10-Q/8-K filings (data.sec.gov, no key)
  - SQLite DB : news_signals, congressional_cluster_signals, macro_indicators
  - FRED      : already in macro_indicators table

Staleness policy:
  Research expires after STALE_DAYS days. Expired rows are set status='expired'
  so the researcher re-runs fresh on them next cycle.
"""

import json
import logging
import sqlite3
import subprocess
import sys
from trading_db import get_sqlite_conn
import time
import requests
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional


def _research_ticker_with_timeout(ticker: str, date_str: str, conn, timeout_secs: int = 45) -> bool:
    """
    Run research_ticker in a subprocess so that any blocking C-level yfinance/network
    call can be hard-killed after timeout_secs. Results are written to the shared DB
    by the subprocess directly (same DB path).
    """
    script = Path(__file__).resolve()
    try:
        result = subprocess.run(
            [sys.executable, str(script), '--ticker', ticker, '--date', date_str],
            timeout=timeout_secs,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            logger.info(f"  ✅ subprocess research OK for {ticker}")
            return True
        else:
            logger.warning(f"  ⚠️  subprocess research failed for {ticker} (rc={result.returncode}): {result.stderr[-300:]}")
            return False
    except subprocess.TimeoutExpired:
        logger.warning(f"  ⏱️  {ticker} research hard-killed after {timeout_secs}s (yfinance hung)")
        return False
    except Exception as e:
        logger.warning(f"  ⚠️  subprocess research error for {ticker}: {e}")
        return False

logger = logging.getLogger(__name__)

SCRIPT_DIR  = Path(__file__).parent.resolve()
DB_PATH     = SCRIPT_DIR / 'trading_data' / 'trading_history.db'
STALE_DAYS  = 3          # research older than this is re-gathered
MAX_TICKERS = 10         # safety cap per run to avoid hammering APIs
SEC_HEADERS = {'User-Agent': 'HighTrade research@hightrade.local'}


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    conn = get_sqlite_conn(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_library_table(conn: sqlite3.Connection):
    """Create stock_research_library table if it doesn't exist, and migrate new columns."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_research_library (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker              TEXT NOT NULL,
            research_date       TEXT NOT NULL,
            -- Price & technicals
            current_price       REAL,
            price_1w_chg_pct    REAL,
            price_1m_chg_pct    REAL,
            price_52w_high      REAL,
            price_52w_low       REAL,
            avg_volume_20d      INTEGER,
            -- Fundamentals
            market_cap          REAL,
            pe_ratio            REAL,
            forward_pe          REAL,
            peg_ratio           REAL,
            price_to_book       REAL,
            profit_margin       REAL,
            revenue_growth_yoy  REAL,
            earnings_growth_yoy REAL,
            debt_to_equity      REAL,
            free_cash_flow      REAL,
            -- Analyst coverage
            analyst_target_mean REAL,
            analyst_target_high REAL,
            analyst_target_low  REAL,
            analyst_buy_count   INTEGER,
            analyst_hold_count  INTEGER,
            analyst_sell_count  INTEGER,
            recommendation_key  TEXT,
            recommendation_mean REAL,
            analyst_count       INTEGER,
            -- Earnings
            next_earnings_date  TEXT,
            last_eps_surprise_pct REAL,
            -- SEC filings
            latest_filing_type  TEXT,
            latest_filing_date  TEXT,
            sec_recent_8k_summary TEXT,
            -- Internal signals
            news_mention_count  INTEGER DEFAULT 0,
            news_sentiment_avg  REAL,
            congressional_signal_strength REAL,
            congressional_buy_count INTEGER DEFAULT 0,
            -- Macro context (snapshot)
            macro_score         REAL,
            market_regime       TEXT,
            vix_level           REAL,
            -- Short interest (data_bridge)
            short_pct_float     REAL,
            shares_short        INTEGER,
            short_ratio         REAL,
            short_date          TEXT,
            -- Options snapshot (data_bridge)
            options_atm_iv_call    REAL,
            options_atm_iv_put     REAL,
            options_put_call_ratio REAL,
            options_total_call_oi  INTEGER,
            options_total_put_oi   INTEGER,
            options_nearest_expiry TEXT,
            -- Pre-market (data_bridge)
            pre_market_price    REAL,
            pre_market_chg_pct  REAL,
            -- Insider activity (data_bridge)
            insider_buys_90d    INTEGER DEFAULT 0,
            insider_sells_90d   INTEGER DEFAULT 0,
            insider_net_sentiment TEXT,
            insider_last_date   TEXT,
            -- Raw blobs for analyst
            yfinance_info_json  TEXT,
            sec_filings_json    TEXT,
            news_signals_json   TEXT,
            insider_txns_json   TEXT,
            news_zero_reason    TEXT,
            -- Status
            status              TEXT DEFAULT 'library_ready',
            error_notes         TEXT,
            created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(ticker, research_date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lib_ticker ON stock_research_library(ticker)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lib_status ON stock_research_library(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lib_date   ON stock_research_library(research_date)")

    # Migrate new columns onto existing tables (safe — silently skips if already present)
    _new_cols = [
        ("recommendation_key",     "TEXT"),
        ("recommendation_mean",    "REAL"),
        ("analyst_count",          "INTEGER"),
        ("vix_level",              "REAL"),
        ("short_pct_float",        "REAL"),
        ("shares_short",           "INTEGER"),
        ("short_ratio",            "REAL"),
        ("short_date",             "TEXT"),
        ("options_atm_iv_call",    "REAL"),
        ("options_atm_iv_put",     "REAL"),
        ("options_put_call_ratio", "REAL"),
        ("options_total_call_oi",  "INTEGER"),
        ("options_total_put_oi",   "INTEGER"),
        ("options_nearest_expiry", "TEXT"),
        ("pre_market_price",       "REAL"),
        ("pre_market_chg_pct",     "REAL"),
        ("insider_buys_90d",       "INTEGER DEFAULT 0"),
        ("insider_sells_90d",      "INTEGER DEFAULT 0"),
        ("insider_net_sentiment",  "TEXT"),
        ("insider_last_date",      "TEXT"),
        ("insider_txns_json",      "TEXT"),
        ("news_zero_reason",       "TEXT"),
    ]
    for col, col_type in _new_cols:
        try:
            conn.execute(f"ALTER TABLE stock_research_library ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass  # Column already exists — safe to skip

    conn.commit()


# ── UW research ───────────────────────────────────────────────────────────────

def _uw_get(path: str, params: dict = None):
    """Fetch from Unusual Whales API. Returns parsed JSON or None on failure."""
    import os, dotenv, pathlib
    env_path = pathlib.Path.home() / ".openclaw" / "creds" / "unusualwhales.env"
    dotenv.load_dotenv(env_path)
    key = os.getenv("UNUSUAL_WHALES_API_KEY", "")
    if not key:
        return None
    headers = {"Authorization": f"Bearer {key}", "UW-CLIENT-API-ID": "100001"}
    url = f"https://api.unusualwhales.com{path}"
    try:
        r = requests.get(url, headers=headers, params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _fetch_yfinance(ticker: str) -> Dict:
    """Pull fundamentals, price, and analyst data from Unusual Whales API."""
    try:
        # Fetch stock state (price, market cap, basic info)
        state_resp = _uw_get(f'/api/stock/{ticker}/stock-state')
        state = state_resp.get('data', {}) if (state_resp and isinstance(state_resp, dict)) else {}

        # Fetch financials
        fin_resp = _uw_get(f'/api/stock/{ticker}/financials')
        fin = {}
        if fin_resp and isinstance(fin_resp, dict):
            fin_data = fin_resp.get('data') or []
            if isinstance(fin_data, list) and fin_data:
                fin = fin_data[0] if isinstance(fin_data[0], dict) else {}
            elif isinstance(fin_data, dict):
                fin = fin_data

        # Fetch earnings history
        earn_resp = _uw_get(f'/api/stock/{ticker}/earnings')
        earn_list = []
        if earn_resp and isinstance(earn_resp, dict):
            earn_data = earn_resp.get('data') or []
            if isinstance(earn_data, list):
                earn_list = earn_data
            elif isinstance(earn_data, dict):
                earn_list = [earn_data]

        # Fetch stock info
        info_resp = _uw_get(f'/api/stock/{ticker}/info')
        info = {}
        if info_resp and isinstance(info_resp, dict):
            info = info_resp.get('data', {}) or {}

        # Extract current price
        price_now = None
        try:
            price_now = float(state.get('last_price') or state.get('prev_close') or 0) or None
        except Exception:
            pass

        # Last EPS surprise from earnings history
        eps_surprise = None
        try:
            if earn_list:
                last_e = earn_list[0]
                surprise = last_e.get('eps_surprise_pct') or last_e.get('surprise_pct')
                if surprise is not None:
                    eps_surprise = float(surprise)
        except Exception:
            pass

        return {
            'current_price':       price_now,
            'price_1w_chg_pct':    None,
            'price_1m_chg_pct':    None,
            'price_52w_high':      _safe_float(state.get('week_52_high') or info.get('week_52_high')),
            'price_52w_low':       _safe_float(state.get('week_52_low') or info.get('week_52_low')),
            'avg_volume_20d':      _safe_int(state.get('avg_volume') or info.get('avg_volume')),
            'market_cap':          _safe_float(state.get('market_cap') or info.get('market_cap')),
            'pe_ratio':            _safe_float(state.get('pe') or fin.get('pe_ratio')),
            'forward_pe':          _safe_float(state.get('forward_pe') or fin.get('forward_pe')),
            'peg_ratio':           _safe_float(fin.get('peg_ratio')),
            'price_to_book':       _safe_float(fin.get('price_to_book')),
            'profit_margin':       _safe_float(fin.get('profit_margin') or fin.get('net_margin')),
            'revenue_growth_yoy':  _safe_float(fin.get('revenue_growth') or fin.get('revenue_growth_yoy')),
            'earnings_growth_yoy': _safe_float(fin.get('earnings_growth') or fin.get('earnings_growth_yoy')),
            'debt_to_equity':      _safe_float(fin.get('debt_to_equity')),
            'free_cash_flow':      _safe_float(fin.get('free_cash_flow')),
            'analyst_target_mean': _safe_float(info.get('analyst_mean_target')),
            'analyst_target_high': _safe_float(info.get('analyst_high_target')),
            'analyst_target_low':  _safe_float(info.get('analyst_low_target')),
            'analyst_buy_count':   _safe_int(info.get('analyst_buy_count')),
            'analyst_hold_count':  _safe_int(info.get('analyst_hold_count')),
            'analyst_sell_count':  _safe_int(info.get('analyst_sell_count')),
            'next_earnings_date':  None,
            'last_eps_surprise_pct': eps_surprise,
            'info':                {**state, **info},
        }
    except Exception as e:
        logger.warning(f"UW research failed for {ticker}: {e}")
        return {'error': str(e)}


def _safe_float(val):
    try:
        return float(val) if val is not None else None
    except Exception:
        return None


def _safe_int(val):
    try:
        return int(val) if val is not None else None
    except Exception:
        return None


# ── SEC EDGAR research ─────────────────────────────────────────────────────────

def _ticker_to_cik(ticker: str) -> Optional[str]:
    """Look up CIK from SEC's company_tickers.json (cached in memory)."""
    if not hasattr(_ticker_to_cik, '_map'):
        try:
            r = requests.get(
                'https://www.sec.gov/files/company_tickers.json',
                headers=SEC_HEADERS, timeout=10
            )
            data = r.json()
            _ticker_to_cik._map = {
                v['ticker'].upper(): str(v['cik_str']).zfill(10)
                for v in data.values()
            }
        except Exception as e:
            logger.warning(f"SEC ticker map download failed: {e}")
            _ticker_to_cik._map = {}
    return _ticker_to_cik._map.get(ticker.upper())


def _fetch_sec_filings(ticker: str) -> Dict:
    """Fetch recent SEC filings for ticker via EDGAR REST API (no key required)."""
    cik = _ticker_to_cik(ticker)
    if not cik:
        return {'error': f'CIK not found for {ticker}'}

    try:
        url = f'https://data.sec.gov/submissions/CIK{cik}.json'
        r = requests.get(url, headers=SEC_HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()

        filings = data.get('filings', {}).get('recent', {})
        forms       = filings.get('form', [])
        dates       = filings.get('filingDate', [])
        descriptions = filings.get('primaryDocument', [])

        # Find the most recent 10-K, 10-Q, 8-K
        result = {
            'latest_10k': None,
            'latest_10q': None,
            'latest_8k':  None,
            'recent_filings': []
        }

        for i, form in enumerate(forms[:50]):
            entry = {
                'form': form,
                'date': dates[i] if i < len(dates) else '',
                'doc':  descriptions[i] if i < len(descriptions) else '',
            }
            result['recent_filings'].append(entry)
            if form == '10-K' and not result['latest_10k']:
                result['latest_10k'] = entry
            elif form == '10-Q' and not result['latest_10q']:
                result['latest_10q'] = entry
            elif form == '8-K' and not result['latest_8k']:
                result['latest_8k'] = entry

        # Latest filing headline
        latest_type = forms[0] if forms else None
        latest_date = dates[0] if dates else None

        # 8-K summary: pull company name + item descriptions
        sec_8k_summary = ''
        if result['latest_8k']:
            sec_8k_summary = (
                f"{result['latest_8k']['form']} filed {result['latest_8k']['date']}: "
                f"{result['latest_8k']['doc']}"
            )

        return {
            'latest_filing_type': latest_type,
            'latest_filing_date': latest_date,
            'sec_recent_8k_summary': sec_8k_summary,
            'filings_json': result,
        }

    except Exception as e:
        logger.warning(f"SEC EDGAR failed for {ticker} (CIK {cik}): {e}")
        return {'error': str(e)}


# ── Internal DB signals ────────────────────────────────────────────────────────

def _fetch_internal_signals(ticker: str, conn: sqlite3.Connection) -> Dict:
    """Pull news mentions, congressional signals, and macro snapshot from our DB."""
    result = {}
    since = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')

    # News mentions (look for ticker in keyword_hits_json)
    try:
        cursor = conn.execute("""
            SELECT timestamp, news_score, sentiment_summary, keyword_hits_json
            FROM news_signals
            WHERE DATE(timestamp) >= ? AND keyword_hits_json LIKE ?
            ORDER BY news_score DESC LIMIT 20
        """, (since, f'%{ticker}%'))
        rows = cursor.fetchall()

        mention_count = len(rows)
        sentiments = []
        for row in rows:
            try:
                hits = json.loads(row['keyword_hits_json'] or '{}')
                if ticker.lower() in [k.lower() for k in hits.keys()]:
                    sentiments.append(row['news_score'])
            except Exception:
                pass

        result['news_mention_count'] = mention_count
        result['news_sentiment_avg'] = (sum(sentiments) / len(sentiments)) if sentiments else None
        result['news_signals_sample'] = [dict(r) for r in rows[:5]]
    except Exception as e:
        logger.warning(f"News signal lookup failed for {ticker}: {e}")
        result['news_mention_count'] = 0

    # Congressional signals
    try:
        cursor = conn.execute("""
            SELECT ticker, buy_count, signal_strength, bipartisan,
                   committee_relevance, politicians_json, created_at
            FROM congressional_cluster_signals
            WHERE UPPER(ticker) = UPPER(?)
            ORDER BY created_at DESC LIMIT 3
        """, (ticker,))
        rows = cursor.fetchall()
        if rows:
            best = dict(rows[0])
            result['congressional_signal_strength'] = best.get('signal_strength', 0)
            result['congressional_buy_count']       = best.get('buy_count', 0)
            result['congressional_rows']            = [dict(r) for r in rows]
        else:
            result['congressional_signal_strength'] = 0
            result['congressional_buy_count']       = 0
    except Exception as e:
        logger.warning(f"Congressional lookup failed for {ticker}: {e}")
        result['congressional_signal_strength'] = 0
        result['congressional_buy_count']       = 0

    # Macro context (latest row)
    try:
        cursor = conn.execute("""
            SELECT macro_score, yield_curve_spread, fed_funds_rate,
                   hy_oas_bps, consumer_sentiment
            FROM macro_indicators ORDER BY created_at DESC LIMIT 1
        """)
        row = cursor.fetchone()
        result['macro_score'] = dict(row)['macro_score'] if row else None
    except Exception:
        result['macro_score'] = None

    return result


# ── Market regime (from latest daily briefing) ─────────────────────────────────

def _get_latest_regime(conn: sqlite3.Connection) -> str:
    """Get the most recent market_regime from daily_briefings."""
    try:
        cursor = conn.execute("""
            SELECT market_regime FROM daily_briefings
            WHERE model_key = 'reasoning'
            ORDER BY date DESC, created_at DESC LIMIT 1
        """)
        row = cursor.fetchone()
        return row['market_regime'] if row else 'unknown'
    except Exception:
        return 'unknown'


# ── Staleness check & expiry ───────────────────────────────────────────────────

def _expire_stale_research(conn: sqlite3.Connection):
    """Mark research older than STALE_DAYS as expired so it gets re-run."""
    cutoff = (datetime.now() - timedelta(days=STALE_DAYS)).strftime('%Y-%m-%d')
    conn.execute("""
        UPDATE stock_research_library
        SET status = 'expired'
        WHERE research_date < ? AND status = 'library_ready'
    """, (cutoff,))
    conn.commit()


# ── Main research function ─────────────────────────────────────────────────────

def research_ticker(ticker: str, date_str: str, conn: sqlite3.Connection) -> bool:
    """
    Run full research pass on a single ticker. Writes to stock_research_library.
    Returns True if successful.
    """
    ticker = ticker.upper().strip()
    logger.info(f"  🔍 Researching {ticker}...")

    errors = []

    # 1. UW fundamentals (via _fetch_yfinance, now UW-backed)
    yf_data = _fetch_yfinance(ticker)
    if 'error' in yf_data:
        errors.append(f"uw_research: {yf_data['error']}")

    # 1b. data_bridge — fills recurring gaps (earnings, short interest, options,
    #     pre-market, VIX, analyst consensus, insider activity, news coverage)
    try:
        import data_bridge
        bridge_data = data_bridge.enrich(ticker, yf_data)
        # Merge bridge results; bridge wins for fields it provides
        yf_data.update(bridge_data)
        logger.debug(f"  🌉 Bridge enriched {ticker}: {list(bridge_data.keys())}")
    except Exception as _be:
        errors.append(f"bridge: {_be}")
        logger.warning(f"  ⚠️  data_bridge failed for {ticker}: {_be}")

    time.sleep(0.5)

    # 2. SEC EDGAR filings
    sec_data = _fetch_sec_filings(ticker)
    if 'error' in sec_data:
        errors.append(f"sec: {sec_data['error']}")

    time.sleep(0.3)

    # 3. Internal DB signals
    internal = _fetch_internal_signals(ticker, conn)

    # 4. Market regime
    regime = _get_latest_regime(conn)

    # ── Assemble and write to DB ──────────────────────────────────────────
    try:
        conn.execute("""
            INSERT OR REPLACE INTO stock_research_library (
                ticker, research_date,
                current_price, price_1w_chg_pct, price_1m_chg_pct,
                price_52w_high, price_52w_low, avg_volume_20d,
                market_cap, pe_ratio, forward_pe, peg_ratio, price_to_book,
                profit_margin, revenue_growth_yoy, earnings_growth_yoy,
                debt_to_equity, free_cash_flow,
                analyst_target_mean, analyst_target_high, analyst_target_low,
                analyst_buy_count, analyst_hold_count, analyst_sell_count,
                recommendation_key, recommendation_mean, analyst_count,
                next_earnings_date, last_eps_surprise_pct,
                latest_filing_type, latest_filing_date, sec_recent_8k_summary,
                news_mention_count, news_sentiment_avg,
                congressional_signal_strength, congressional_buy_count,
                macro_score, market_regime, vix_level,
                short_pct_float, shares_short, short_ratio, short_date,
                options_atm_iv_call, options_atm_iv_put, options_put_call_ratio,
                options_total_call_oi, options_total_put_oi, options_nearest_expiry,
                pre_market_price, pre_market_chg_pct,
                insider_buys_90d, insider_sells_90d, insider_net_sentiment, insider_last_date,
                yfinance_info_json, sec_filings_json, news_signals_json,
                insider_txns_json, news_zero_reason,
                status, error_notes
            ) VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
        """, (
            ticker, date_str,
            yf_data.get('current_price'),
            yf_data.get('price_1w_chg_pct'),
            yf_data.get('price_1m_chg_pct'),
            yf_data.get('price_52w_high'),
            yf_data.get('price_52w_low'),
            yf_data.get('avg_volume_20d'),
            yf_data.get('market_cap'),
            yf_data.get('pe_ratio'),
            yf_data.get('forward_pe'),
            yf_data.get('peg_ratio'),
            yf_data.get('price_to_book'),
            yf_data.get('profit_margin'),
            yf_data.get('revenue_growth_yoy'),
            yf_data.get('earnings_growth_yoy'),
            yf_data.get('debt_to_equity'),
            yf_data.get('free_cash_flow'),
            yf_data.get('analyst_target_mean'),
            yf_data.get('analyst_target_high'),
            yf_data.get('analyst_target_low'),
            yf_data.get('analyst_buy_count', 0),
            yf_data.get('analyst_hold_count', 0),
            yf_data.get('analyst_sell_count', 0),
            yf_data.get('recommendation_key'),
            yf_data.get('recommendation_mean'),
            yf_data.get('analyst_count'),
            yf_data.get('next_earnings_date'),
            yf_data.get('last_eps_surprise_pct'),
            sec_data.get('latest_filing_type'),
            sec_data.get('latest_filing_date'),
            sec_data.get('sec_recent_8k_summary'),
            internal.get('news_mention_count', 0),
            internal.get('news_sentiment_avg'),
            internal.get('congressional_signal_strength', 0),
            internal.get('congressional_buy_count', 0),
            internal.get('macro_score'),
            regime,
            yf_data.get('vix_level'),
            yf_data.get('short_pct_float'),
            yf_data.get('shares_short'),
            yf_data.get('short_ratio'),
            yf_data.get('short_date'),
            yf_data.get('options_atm_iv_call'),
            yf_data.get('options_atm_iv_put'),
            yf_data.get('options_put_call_ratio'),
            yf_data.get('options_total_call_oi'),
            yf_data.get('options_total_put_oi'),
            yf_data.get('options_nearest_expiry'),
            yf_data.get('pre_market_price'),
            yf_data.get('pre_market_chg_pct'),
            yf_data.get('insider_buys_90d', 0),
            yf_data.get('insider_sells_90d', 0),
            yf_data.get('insider_net_sentiment'),
            yf_data.get('insider_last_date'),
            json.dumps(yf_data.get('info', {})),
            json.dumps(sec_data.get('filings_json', {})),
            json.dumps(internal.get('news_signals_sample', [])),
            yf_data.get('insider_txns_json'),
            yf_data.get('news_zero_reason'),
            'library_ready' if not errors else 'partial',
            '; '.join(errors) if errors else None,
        ))
        conn.commit()

        status = 'library_ready' if not errors else f'partial ({len(errors)} errors)'
        logger.info(f"  ✅ {ticker} researched → {status} | price=${yf_data.get('current_price','N/A')} mcap={yf_data.get('market_cap','N/A')}")
        return True

    except Exception as e:
        logger.error(f"  ❌ DB write failed for {ticker}: {e}")
        return False


# ── Pipeline entry point ───────────────────────────────────────────────────────

def run_research_cycle() -> List[str]:
    """
    Main pipeline function called by orchestrator.

    1. Expire stale research
    2. Fetch pending tickers from acquisition_watchlist
    3. Research each, write to stock_research_library
    4. Update acquisition_watchlist.status → 'researched'

    Returns list of tickers successfully researched this cycle.
    """
    date_str = datetime.now().strftime('%Y-%m-%d')
    logger.info(f"🔬 Acquisition Researcher: starting cycle for {date_str}")

    conn = _get_conn()
    try:
        _ensure_library_table(conn)
        _expire_stale_research(conn)

        # Pull pending tickers (most recent dates first, capped)
        try:
            cursor = conn.execute("""
                SELECT ticker, date_added, entry_conditions, model_confidence
                FROM acquisition_watchlist
                WHERE status = 'pending'
                ORDER BY date_added DESC, model_confidence DESC
                LIMIT ?
            """, (MAX_TICKERS,))
            pending = [dict(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to fetch pending watchlist: {e}")
            return []

        if not pending:
            logger.info("  📭 No pending tickers in acquisition_watchlist")
            return []

        logger.info(f"  📋 {len(pending)} tickers pending research: {[r['ticker'] for r in pending]}")

        researched = []
        for row in pending:
            ticker = row['ticker']
            success = _research_ticker_with_timeout(ticker, date_str, conn, timeout_secs=45)

            if success:
                # Mark as researched in watchlist — prepend status prefix but preserve any
                # existing notes (e.g. exit_review tags set by the missing-framework monitor)
                conn.execute("""
                    UPDATE acquisition_watchlist
                    SET status = 'researched',
                        notes = CASE
                            WHEN notes IS NULL OR notes = '' THEN ?
                            ELSE ? || ' | ' || notes
                        END
                    WHERE ticker = ? AND status = 'pending'
                """, (f"Researched {date_str}", f"Researched {date_str}", ticker))
                conn.commit()
                researched.append(ticker)
            else:
                # Mark as error but don't block pipeline
                conn.execute("""
                    UPDATE acquisition_watchlist
                    SET status = 'research_error',
                        notes = CASE
                            WHEN notes IS NULL OR notes = '' THEN ?
                            ELSE ? || ' | ' || notes
                        END
                    WHERE ticker = ? AND status = 'pending'
                """, (f"Research failed {date_str}", f"Research failed {date_str}", ticker))
                conn.commit()

            # Polite delay between tickers
            time.sleep(1.0)

        logger.info(f"✅ Research cycle complete: {len(researched)}/{len(pending)} tickers ready → {researched}")
        return researched
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == '__main__':
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )

    if '--ticker' in sys.argv:
        idx = sys.argv.index('--ticker')
        ticker = sys.argv[idx + 1].upper()
        date_str = datetime.now().strftime('%Y-%m-%d')
        if '--date' in sys.argv:
            didx = sys.argv.index('--date')
            date_str = sys.argv[didx + 1]
        print(f"\n🔍 Single-ticker research: {ticker} ({date_str})")
        conn = _get_conn()
        _ensure_library_table(conn)
        success = research_ticker(ticker, date_str, conn)
        # Also update watchlist status when run as subprocess
        if success:
            conn.execute("""
                UPDATE acquisition_watchlist
                SET status = 'researched',
                    notes = CASE
                        WHEN notes IS NULL OR notes = '' THEN ?
                        ELSE ? || ' | ' || notes
                    END
                WHERE ticker = ? AND status = 'pending'
            """, (f"Researched {date_str}", f"Researched {date_str}", ticker))
        else:
            conn.execute("""
                UPDATE acquisition_watchlist
                SET status = 'research_error',
                    notes = CASE
                        WHEN notes IS NULL OR notes = '' THEN ?
                        ELSE ? || ' | ' || notes
                    END
                WHERE ticker = ? AND status = 'pending'
            """, (f"Research failed {date_str}", f"Research failed {date_str}", ticker))
        conn.commit()
        conn.close()
        print(f"{'✅ Done' if success else '❌ Failed'}")
        sys.exit(0 if success else 1)
    else:
        print(f"\n🔬 Acquisition Researcher — full cycle")
        tickers = run_research_cycle()
        print(f"\nResearched: {tickers}")
