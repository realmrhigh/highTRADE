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

Data sources (all free, no extra API keys):
  - yfinance  : price history, fundamentals, analyst targets, earnings
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
import time
import requests
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

SCRIPT_DIR  = Path(__file__).parent.resolve()
DB_PATH     = SCRIPT_DIR / 'trading_data' / 'trading_history.db'
STALE_DAYS  = 3          # research older than this is re-gathered
MAX_TICKERS = 10         # safety cap per run to avoid hammering APIs
SEC_HEADERS = {'User-Agent': 'HighTrade research@hightrade.local'}


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_library_table(conn: sqlite3.Connection):
    """Create stock_research_library table if it doesn't exist."""
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
            -- Raw blobs for analyst
            yfinance_info_json  TEXT,
            sec_filings_json    TEXT,
            news_signals_json   TEXT,
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
    conn.commit()


# ── yfinance research ──────────────────────────────────────────────────────────

def _fetch_yfinance(ticker: str) -> Dict:
    """Pull fundamentals, price history, analyst data from yfinance."""
    try:
        import yfinance as yf
        stock = yf.Ticker(ticker)

        info = stock.info or {}

        # Price history — last 30 trading days
        hist = stock.history(period='1mo', auto_adjust=True)
        price_now  = float(hist['Close'].iloc[-1])  if len(hist) > 0 else None
        price_1w   = float(hist['Close'].iloc[-6])  if len(hist) >= 6 else None
        price_1m   = float(hist['Close'].iloc[0])   if len(hist) > 0 else None

        chg_1w = ((price_now - price_1w) / price_1w * 100) if (price_now and price_1w) else None
        chg_1m = ((price_now - price_1m) / price_1m * 100) if (price_now and price_1m) else None

        avg_vol = int(hist['Volume'].tail(20).mean()) if len(hist) >= 20 else None

        # Earnings calendar
        next_earnings = None
        try:
            cal = stock.calendar
            if cal is not None and not cal.empty:
                # calendar is a DataFrame with dates as columns
                if 'Earnings Date' in cal.index:
                    ed = cal.loc['Earnings Date']
                    if hasattr(ed, 'iloc'):
                        next_earnings = str(ed.iloc[0])[:10]
                    else:
                        next_earnings = str(ed)[:10]
        except Exception:
            pass

        # Last EPS surprise
        eps_surprise = None
        try:
            earnings_hist = stock.earnings_history
            if earnings_hist is not None and not earnings_hist.empty:
                last = earnings_hist.iloc[0]
                if 'Surprise(%)' in last:
                    eps_surprise = float(last['Surprise(%)'])
        except Exception:
            pass

        # Analyst targets
        targets = {}
        try:
            rec = stock.analyst_price_targets
            if rec is not None:
                targets = {
                    'mean': float(rec.get('mean', 0) or 0),
                    'high': float(rec.get('high', 0) or 0),
                    'low':  float(rec.get('low',  0) or 0),
                }
        except Exception:
            pass

        # Analyst recommendations count
        buy_c = hold_c = sell_c = 0
        try:
            recs = stock.upgrades_downgrades
            if recs is not None and not recs.empty:
                recent = recs.tail(30)
                buy_c  = int((recent['ToGrade'].str.lower().str.contains('buy|outperform|overweight')).sum())
                hold_c = int((recent['ToGrade'].str.lower().str.contains('hold|neutral|market')).sum())
                sell_c = int((recent['ToGrade'].str.lower().str.contains('sell|underperform|underweight')).sum())
        except Exception:
            pass

        return {
            'current_price':       price_now,
            'price_1w_chg_pct':    chg_1w,
            'price_1m_chg_pct':    chg_1m,
            'price_52w_high':      info.get('fiftyTwoWeekHigh'),
            'price_52w_low':       info.get('fiftyTwoWeekLow'),
            'avg_volume_20d':      avg_vol,
            'market_cap':          info.get('marketCap'),
            'pe_ratio':            info.get('trailingPE'),
            'forward_pe':          info.get('forwardPE'),
            'peg_ratio':           info.get('pegRatio'),
            'price_to_book':       info.get('priceToBook'),
            'profit_margin':       info.get('profitMargins'),
            'revenue_growth_yoy':  info.get('revenueGrowth'),
            'earnings_growth_yoy': info.get('earningsGrowth'),
            'debt_to_equity':      info.get('debtToEquity'),
            'free_cash_flow':      info.get('freeCashflow'),
            'analyst_target_mean': targets.get('mean'),
            'analyst_target_high': targets.get('high'),
            'analyst_target_low':  targets.get('low'),
            'analyst_buy_count':   buy_c,
            'analyst_hold_count':  hold_c,
            'analyst_sell_count':  sell_c,
            'next_earnings_date':  next_earnings,
            'last_eps_surprise_pct': eps_surprise,
            'info':                info,
        }
    except Exception as e:
        logger.warning(f"yfinance failed for {ticker}: {e}")
        return {'error': str(e)}


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

    # 1. yfinance fundamentals
    yf_data = _fetch_yfinance(ticker)
    if 'error' in yf_data:
        errors.append(f"yfinance: {yf_data['error']}")

    # Small delay to be polite to APIs
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
                next_earnings_date, last_eps_surprise_pct,
                latest_filing_type, latest_filing_date, sec_recent_8k_summary,
                news_mention_count, news_sentiment_avg,
                congressional_signal_strength, congressional_buy_count,
                macro_score, market_regime,
                yfinance_info_json, sec_filings_json, news_signals_json,
                status, error_notes
            ) VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                ?,?,?,?,?,?,?,?,?,?,?
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
            json.dumps(yf_data.get('info', {})),
            json.dumps(sec_data.get('filings_json', {})),
            json.dumps(internal.get('news_signals_sample', [])),
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
        conn.close()
        return []

    if not pending:
        logger.info("  📭 No pending tickers in acquisition_watchlist")
        conn.close()
        return []

    logger.info(f"  📋 {len(pending)} tickers pending research: {[r['ticker'] for r in pending]}")

    researched = []
    for row in pending:
        ticker = row['ticker']
        success = research_ticker(ticker, date_str, conn)

        if success:
            # Mark as researched in watchlist
            conn.execute("""
                UPDATE acquisition_watchlist
                SET status = 'researched', notes = ?
                WHERE ticker = ? AND status = 'pending'
            """, (f"Researched {date_str}", ticker))
            conn.commit()
            researched.append(ticker)
        else:
            # Mark as error but don't block pipeline
            conn.execute("""
                UPDATE acquisition_watchlist
                SET status = 'research_error', notes = ?
                WHERE ticker = ? AND status = 'pending'
            """, (f"Research failed {date_str}", ticker))
            conn.commit()

        # Polite delay between tickers
        time.sleep(1.0)

    conn.close()
    logger.info(f"✅ Research cycle complete: {len(researched)}/{len(pending)} tickers ready → {researched}")
    return researched


if __name__ == '__main__':
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )

    if '--ticker' in sys.argv:
        idx = sys.argv.index('--ticker')
        ticker = sys.argv[idx + 1].upper()
        print(f"\n🔍 Single-ticker research: {ticker}")
        conn = _get_conn()
        _ensure_library_table(conn)
        success = research_ticker(ticker, datetime.now().strftime('%Y-%m-%d'), conn)
        conn.close()
        print(f"{'✅ Done' if success else '❌ Failed'}")
    else:
        print(f"\n🔬 Acquisition Researcher — full cycle")
        tickers = run_research_cycle()
        print(f"\nResearched: {tickers}")
