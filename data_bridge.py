#!/usr/bin/env python3
"""
data_bridge.py — Fills recurring AI-identified data gaps for the HighTrade
acquisition pipeline.

Gap coverage (from weekly recurring-gaps analysis):
  ┌──────────────────────────────────────┬───────┬────────────────────────────┐
  │ Gap                                  │ Freq  │ Source                     │
  ├──────────────────────────────────────┼───────┼────────────────────────────┤
  │ Next earnings date                   │  ×14  │ yfinance calendar (fixed)  │
  │ Short interest % of float            │   ×9  │ yfinance info dict         │
  │ Options OI / IV / flow               │   ×4  │ yfinance options chain     │
  │ Insider buying / selling (90d)       │   ×4  │ yfinance insider_txns      │
  │ Pre-market price / volume            │   ×2  │ yfinance fast_info         │
  │ Current VIX level                    │   ×2  │ yfinance ^VIX              │
  │ Analyst price targets / ratings      │   ×2  │ yfinance info dict         │
  │ News mention count (zero-coverage)   │   ×2  │ local DB                   │
  └──────────────────────────────────────┴───────┴────────────────────────────┘

Design principles:
  - Every function returns a dict, never raises.
  - All fields are None/0 on failure, never absent.
  - yfinance 404 noise for ETFs is suppressed at the logger level.
  - enrich() is the single entry-point — only fetches what's missing.
"""

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent.resolve()
DB_PATH    = SCRIPT_DIR / 'trading_data' / 'trading_history.db'

# ETFs never have earnings / insider data — skip those calls silently.
_ETF_LIKE = {
    'SPY','QQQ','IWM','DIA','GLD','SLV','USO','TLT','IEF','SHY',
    'HYG','LQD','VXX','UVXY','SVXY','SQQQ','TQQQ','SPXL','SPXS',
    'XLK','XLE','XLF','XLV','XLY','XLP','XLI','XLB','XLU','XLC',
    'XLRE','ITA','XAR','IHI','SOXX','SMH','ARKK','ARKG','ARKW',
    'IBB','XBI','GDX','GDXJ','EEM','EFA','VWO','AGG','BND',
}


def _silence_yf():
    """Return the yfinance logger and its current level (for restore)."""
    import logging as _lm
    lg = _lm.getLogger('yfinance')
    return lg, lg.level


# ── 1. Earnings date ───────────────────────────────────────────────────────────

def get_earnings_date(ticker: str) -> Optional[str]:
    """
    Robust multi-method earnings date lookup.

    Tries in order:
      1. stock.calendar  → dict  (yfinance ≥0.2.x)
      2. stock.calendar  → DataFrame (legacy)
      3. stock.earnings_dates → DataFrame (future rows have NaN EPS)
    Returns ISO date string or None.
    """
    if ticker.upper() in _ETF_LIKE:
        return None
    try:
        import yfinance as yf
        yf_log, yf_lvl = _silence_yf()
        stock = yf.Ticker(ticker)
        today = date.today()

        # ── Method 1: modern dict-based calendar ─────────────────────────
        try:
            yf_log.setLevel(logging.CRITICAL)
            cal = stock.calendar
            yf_log.setLevel(yf_lvl)

            if isinstance(cal, dict) and 'Earnings Date' in cal:
                dates = cal['Earnings Date']
                if not hasattr(dates, '__iter__'):
                    dates = [dates]
                for d in dates:
                    try:
                        dd = d.date() if hasattr(d, 'date') else date.fromisoformat(str(d)[:10])
                        if dd >= today:
                            return str(dd)
                    except Exception:
                        pass
            elif cal is not None and hasattr(cal, 'empty') and not cal.empty:
                # Legacy DataFrame path
                if 'Earnings Date' in cal.index:
                    ed = cal.loc['Earnings Date']
                    raw = ed.iloc[0] if hasattr(ed, 'iloc') else ed
                    return str(raw)[:10]
        except Exception:
            yf_log.setLevel(yf_lvl)

        # ── Method 2: earnings_dates DataFrame ───────────────────────────
        try:
            yf_log.setLevel(logging.CRITICAL)
            ed_df = stock.earnings_dates
            yf_log.setLevel(yf_lvl)
            if ed_df is not None and not ed_df.empty:
                future = ed_df[ed_df.index.tz_localize(None).date >= today]  # type: ignore[operator]
                if not future.empty:
                    return str(future.index[0].date())
        except Exception:
            yf_log.setLevel(yf_lvl)

    except Exception as e:
        logger.debug(f"get_earnings_date({ticker}): {e}")
    return None


# ── 2. Short interest (already in .info — just needs proper extraction) ────────

def get_short_interest(info: dict) -> dict:
    """
    Extract short interest fields from an already-fetched yfinance info dict.
    No additional API call needed.
    """
    raw_pct   = info.get('shortPercentOfFloat')
    raw_shares = info.get('sharesShort')
    raw_ratio  = info.get('shortRatio')
    raw_date   = info.get('dateShortInterest')

    short_pct = float(raw_pct) if raw_pct is not None else None

    short_date = None
    if raw_date:
        try:
            short_date = datetime.fromtimestamp(raw_date).strftime('%Y-%m-%d')
        except Exception:
            pass

    return {
        'short_pct_float':  short_pct,
        'shares_short':     int(raw_shares) if raw_shares else None,
        'short_ratio':      float(raw_ratio) if raw_ratio is not None else None,
        'short_date':       short_date,
    }


# ── 3. Options snapshot ────────────────────────────────────────────────────────

def get_options_snapshot(ticker: str, current_price: Optional[float] = None) -> dict:
    """
    Fetch options chain for the nearest expiry.
    Returns ATM implied vol, put/call OI ratio, and total open interest.
    """
    empty = {
        'options_atm_iv_call':     None,
        'options_atm_iv_put':      None,
        'options_put_call_ratio':  None,
        'options_total_call_oi':   None,
        'options_total_put_oi':    None,
        'options_nearest_expiry':  None,
    }
    try:
        import yfinance as yf
        yf_log, yf_lvl = _silence_yf()

        stock = yf.Ticker(ticker)

        yf_log.setLevel(logging.CRITICAL)
        expirations = stock.options
        yf_log.setLevel(yf_lvl)

        if not expirations:
            return empty

        # Use nearest expiry that is at least 5 days out (avoid 0-DTE noise)
        today = date.today()
        chosen = None
        for exp in expirations:
            try:
                exp_date = date.fromisoformat(exp)
                if (exp_date - today).days >= 5:
                    chosen = exp
                    break
            except Exception:
                pass
        if not chosen:
            chosen = expirations[0]

        yf_log.setLevel(logging.CRITICAL)
        chain = stock.option_chain(chosen)
        yf_log.setLevel(yf_lvl)

        calls = chain.calls
        puts  = chain.puts

        if calls.empty and puts.empty:
            return empty

        # Total OI — on weekends yfinance may return all NaN; treat that as unavailable
        total_call_oi = int(calls['openInterest'].fillna(0).sum()) if not calls.empty else 0
        total_put_oi  = int(puts['openInterest'].fillna(0).sum())  if not puts.empty else 0
        # If OI is zero, data is likely stale (weekend/holiday) — don't compute ratio
        pcr = (total_put_oi / total_call_oi) if total_call_oi > 100 else None

        # ATM IV — find the strike closest to current_price
        atm_iv_call = atm_iv_put = None
        if current_price:
            if not calls.empty and 'strike' in calls.columns:
                calls = calls.copy()
                calls['_dist'] = (calls['strike'] - current_price).abs()
                atm_call_row = calls.nsmallest(1, '_dist')
                if not atm_call_row.empty and 'impliedVolatility' in atm_call_row.columns:
                    atm_iv_call = float(atm_call_row['impliedVolatility'].iloc[0])

            if not puts.empty and 'strike' in puts.columns:
                puts = puts.copy()
                puts['_dist'] = (puts['strike'] - current_price).abs()
                atm_put_row = puts.nsmallest(1, '_dist')
                if not atm_put_row.empty and 'impliedVolatility' in atm_put_row.columns:
                    atm_iv_put = float(atm_put_row['impliedVolatility'].iloc[0])

        return {
            'options_atm_iv_call':    round(atm_iv_call, 4) if atm_iv_call else None,
            'options_atm_iv_put':     round(atm_iv_put,  4) if atm_iv_put  else None,
            'options_put_call_ratio': round(pcr, 3)         if pcr         else None,
            'options_total_call_oi':  total_call_oi,
            'options_total_put_oi':   total_put_oi,
            'options_nearest_expiry': chosen,
        }

    except Exception as e:
        logger.debug(f"get_options_snapshot({ticker}): {e}")
        return empty


# ── 4. Pre-market data ─────────────────────────────────────────────────────────

def get_premarket(ticker: str) -> dict:
    """
    Fetch pre-market price and % change via yfinance fast_info.
    Volume is not reliably available pre-market via yfinance, so we report
    what we can.
    """
    empty = {'pre_market_price': None, 'pre_market_chg_pct': None}
    try:
        import yfinance as yf
        fi = yf.Ticker(ticker).fast_info
        pre   = fi.get('preMarketPrice')
        prev  = fi.get('regularMarketPreviousClose') or fi.get('previousClose')
        chg_pct = None
        if pre and prev and prev != 0:
            chg_pct = round((pre - prev) / prev * 100, 3)
        return {
            'pre_market_price':   round(float(pre), 4) if pre else None,
            'pre_market_chg_pct': chg_pct,
        }
    except Exception as e:
        logger.debug(f"get_premarket({ticker}): {e}")
        return empty


# ── 5. VIX ────────────────────────────────────────────────────────────────────

def get_vix() -> Optional[float]:
    """Fetch current (or last-close) VIX level via yfinance."""
    try:
        import yfinance as yf
        fi = yf.Ticker('^VIX').fast_info
        v  = fi.get('regularMarketPrice') or fi.get('lastPrice')
        return round(float(v), 2) if v else None
    except Exception as e:
        logger.debug(f"get_vix(): {e}")
        return None


# ── 6. Analyst targets (fallback to info dict) ─────────────────────────────────

def get_analyst_info(info: dict) -> dict:
    """
    Extract analyst consensus from an already-fetched yfinance info dict.
    Falls back gracefully; complements the analyst_price_targets call in
    the researcher.
    """
    rec_mean = info.get('recommendationMean')   # 1=Strong Buy … 5=Strong Sell
    rec_key  = info.get('recommendationKey', '') # 'buy', 'hold', 'sell', etc.
    n_analysts = info.get('numberOfAnalystOpinions')

    # Derive human-readable label from recommendationMean if key is absent
    if not rec_key and rec_mean is not None:
        rm = float(rec_mean)
        if rm <= 1.5:   rec_key = 'strong_buy'
        elif rm <= 2.5: rec_key = 'buy'
        elif rm <= 3.5: rec_key = 'hold'
        elif rm <= 4.5: rec_key = 'underperform'
        else:           rec_key = 'sell'

    return {
        'recommendation_key':  rec_key or None,
        'recommendation_mean': float(rec_mean) if rec_mean is not None else None,
        'analyst_count':       int(n_analysts)  if n_analysts  else None,
        # Fallback target prices from info (used if analyst_price_targets fails)
        'target_mean_fallback': info.get('targetMeanPrice'),
        'target_high_fallback': info.get('targetHighPrice'),
        'target_low_fallback':  info.get('targetLowPrice'),
    }


# ── 7. Insider activity ────────────────────────────────────────────────────────

def get_insider_activity(ticker: str, days: int = 90) -> dict:
    """
    Fetch recent insider transactions (Form 4) via yfinance.
    Returns buy/sell counts and the most recent transaction date.
    """
    empty = {
        'insider_buys_90d':    0,
        'insider_sells_90d':   0,
        'insider_net_sentiment': 'neutral',
        'insider_last_date':   None,
        'insider_txns_json':   None,
    }
    if ticker.upper() in _ETF_LIKE:
        return empty
    try:
        import yfinance as yf
        import pandas as pd
        yf_log, yf_lvl = _silence_yf()

        yf_log.setLevel(logging.CRITICAL)
        txns = yf.Ticker(ticker).insider_transactions
        yf_log.setLevel(yf_lvl)

        if txns is None or (hasattr(txns, 'empty') and txns.empty):
            return empty

        # Normalise column names (yfinance uses different casing across versions)
        txns.columns = [c.strip().lower().replace(' ', '_') for c in txns.columns]

        # Find date column
        date_col = next((c for c in txns.columns if 'date' in c), None)
        if date_col:
            cutoff = datetime.now() - timedelta(days=days)
            try:
                txns[date_col] = pd.to_datetime(txns[date_col], utc=True, errors='coerce')
                txns = txns[txns[date_col] >= pd.Timestamp(cutoff, tz='UTC')]
            except Exception:
                pass

        # Find transaction type column
        txn_col = next(
            (c for c in txns.columns if c in ('transaction', 'text', 'type', 'description')),
            None
        )

        # Prefer the column with more non-empty content
        if txn_col == 'transaction':
            non_empty = txns['transaction'].fillna('').str.strip().ne('').sum()
            if non_empty < len(txns) * 0.3 and 'text' in txns.columns:
                txn_col = 'text'

        buys = sells = 0
        if txn_col:
            for val in txns[txn_col].fillna('').str.lower():
                # Open-market purchases only — exclude grants, awards, gifts (compensation)
                is_buy = ('purchase' in val or 'open market buy' in val)
                is_comp = any(kw in val for kw in ('grant', 'award', 'gift', 'stock option'))
                is_sell = any(kw in val for kw in ('sale', 'sell', 'disposition', 'exercise'))
                if is_buy and not is_comp:
                    buys += 1
                elif is_sell:
                    sells += 1

        sentiment = 'neutral'
        if buys > sells * 1.5:   sentiment = 'bullish'
        elif sells > buys * 1.5: sentiment = 'bearish'

        # Most recent date
        last_date = None
        if date_col and not txns.empty:
            try:
                last_date = str(txns[date_col].max().date())
            except Exception:
                pass

        # Compact JSON of top-5 transactions for the analyst blob
        txn_sample = None
        try:
            sample = txns.head(5)
            txn_sample = json.dumps(sample.to_dict(orient='records'), default=str)
        except Exception:
            pass

        return {
            'insider_buys_90d':    buys,
            'insider_sells_90d':   sells,
            'insider_net_sentiment': sentiment,
            'insider_last_date':   last_date,
            'insider_txns_json':   txn_sample,
        }

    except Exception as e:
        logger.debug(f"get_insider_activity({ticker}): {e}")
        return empty


# ── 8. News mention count (local DB cross-check) ──────────────────────────────

def get_news_mention_count(ticker: str, days: int = 30) -> dict:
    """
    Count how many news_signals cycles mentioned this ticker in articles_full_json.
    Returns count + reason string if zero.
    """
    try:
        import sqlite3
        since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.execute("""
            SELECT COUNT(*) FROM news_signals
            WHERE DATE(timestamp) >= ?
              AND (
                    articles_full_json LIKE ?
                 OR keyword_hits_json  LIKE ?
              )
        """, (since, f'%"{ticker}"%', f'%"{ticker.lower()}"%'))
        count = cursor.fetchone()[0]

        # Also check total cycles in window to give context for zero coverage
        total = conn.execute(
            "SELECT COUNT(*) FROM news_signals WHERE DATE(timestamp) >= ?", (since,)
        ).fetchone()[0]
        conn.close()

        zero_reason = None
        if count == 0 and total > 0:
            zero_reason = (
                f"No mentions across {total} monitored cycles in last {days}d — "
                "ticker may be too small-cap for our RSS feeds, or recently listed."
            )

        return {'news_mention_count_bridge': count, 'news_zero_reason': zero_reason}
    except Exception as e:
        logger.debug(f"get_news_mention_count({ticker}): {e}")
        return {'news_mention_count_bridge': None, 'news_zero_reason': None}


# ── Master enrichment function ─────────────────────────────────────────────────

def enrich(ticker: str, existing: dict) -> dict:
    """
    Single entry-point for the acquisition pipeline.

    Accepts an existing research dict (from _fetch_yfinance + DB signals)
    and returns a new dict with all gap fields added.
    Only fetches what is actually missing (None / 0) to avoid redundant calls.

    Typical call from acquisition_researcher.research_ticker():
        bridge_data = data_bridge.enrich(ticker, yf_data)
    """
    ticker = ticker.upper().strip()
    result = {}

    info = existing.get('info', {}) or {}

    # ── 1. Earnings date (if still missing) ──────────────────────────────
    if not existing.get('next_earnings_date'):
        result['next_earnings_date'] = get_earnings_date(ticker)
        if result['next_earnings_date']:
            logger.debug(f"  🗓  {ticker} earnings bridged: {result['next_earnings_date']}")

    # ── 2. Short interest (from already-fetched info dict) ────────────────
    si = get_short_interest(info)
    result.update(si)

    # ── 3. Options snapshot ───────────────────────────────────────────────
    opts = get_options_snapshot(ticker, existing.get('current_price'))
    result.update(opts)

    # ── 4. Pre-market ─────────────────────────────────────────────────────
    pm = get_premarket(ticker)
    result.update(pm)

    # ── 5. VIX ───────────────────────────────────────────────────────────
    result['vix_level'] = get_vix()

    # ── 6. Analyst consensus (from info dict) ────────────────────────────
    ai = get_analyst_info(info)
    result.update(ai)
    # Back-fill analyst targets if the primary fetch came up empty
    if not existing.get('analyst_target_mean') and ai.get('target_mean_fallback'):
        result['analyst_target_mean'] = ai['target_mean_fallback']
        result['analyst_target_high'] = ai['target_high_fallback']
        result['analyst_target_low']  = ai['target_low_fallback']

    # ── 7. Insider activity ───────────────────────────────────────────────
    insider = get_insider_activity(ticker)
    result.update(insider)

    # ── 8. News mention cross-check ───────────────────────────────────────
    news = get_news_mention_count(ticker)
    result.update(news)

    return result


# ── CLI quick-test ─────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import sys, pprint
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    ticker = sys.argv[1].upper() if len(sys.argv) > 1 else 'AAPL'
    print(f'\n🔍 data_bridge quick-test: {ticker}\n')

    import yfinance as yf
    info = yf.Ticker(ticker).info or {}
    existing = {'info': info, 'current_price': info.get('regularMarketPrice')}

    result = enrich(ticker, existing)
    pprint.pprint(result)
