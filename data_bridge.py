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
  │ After-hours price action             │   ×2  │ yfinance prepost=True      │
  │ GLD fund flow proxy                  │   ×2  │ yfinance volume×price      │
  │ Central bank gold purchasing data    │   ×2  │ yfinance GC=F + FRED       │
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


def get_xle_spy_rs() -> Optional[dict]:
    """
    Fetch XLE vs SPY relative strength ratio (1-day and 5-day).
    Returns dict with xle_spy_ratio_1d, xle_spy_ratio_5d, xle_pct_1d, spy_pct_1d, or None.
    Added 2026-04-06 to resolve recurring 'energy sector relative strength' gap.
    """
    try:
        import yfinance as yf
        import pandas as pd
        data = yf.download(['XLE', 'SPY'], period='10d', interval='1d', progress=False)['Close']
        if data.empty or 'XLE' not in data or 'SPY' not in data:
            return None
        xle = data['XLE'].dropna()
        spy = data['SPY'].dropna()
        if len(xle) < 2 or len(spy) < 2:
            return None
        xle_1d = float((xle.iloc[-1] / xle.iloc[-2] - 1) * 100)
        spy_1d = float((spy.iloc[-1] / spy.iloc[-2] - 1) * 100)
        xle_5d = float((xle.iloc[-1] / xle.iloc[max(0, len(xle)-6)] - 1) * 100) if len(xle) >= 6 else None
        spy_5d = float((spy.iloc[-1] / spy.iloc[max(0, len(spy)-6)] - 1) * 100) if len(spy) >= 6 else None
        rs_1d = round(xle_1d - spy_1d, 3)
        rs_5d = round(xle_5d - spy_5d, 3) if xle_5d is not None and spy_5d is not None else None
        return {
            'xle_pct_1d': round(xle_1d, 3),
            'spy_pct_1d': round(spy_1d, 3),
            'xle_spy_rs_1d': rs_1d,   # positive = XLE outperforming SPY
            'xle_spy_rs_5d': rs_5d,
        }
    except Exception as e:
        logger.debug(f"get_xle_spy_rs(): {e}")
        return None


# ── 6b. After-hours / extended price ─────────────────────────────────────────

def get_after_hours_price(ticker: str = 'SPY') -> dict:
    """
    Fetch after-hours (post-market) or pre-market price via yfinance fast_info.
    Falls back to 1-hour history with prepost=True when fast_info is unavailable.
    Added to resolve recurring 'live after-hours price action' gap.
    """
    empty = {'after_hours_price': None, 'after_hours_chg_pct': None,
             'after_hours_type': None, 'after_hours_timestamp': None}
    try:
        import yfinance as yf
        from datetime import datetime as _dt
        t = yf.Ticker(ticker)
        fi = t.fast_info
        prev_close = fi.get('regularMarketPreviousClose') or fi.get('previousClose')
        reg_price  = fi.get('regularMarketPrice')

        # Check post-market first
        post = fi.get('postMarketPrice')
        if post:
            chg_pct = round((post - reg_price) / reg_price * 100, 3) if reg_price else None
            return {
                'after_hours_price':     round(float(post), 4),
                'after_hours_chg_pct':  chg_pct,
                'after_hours_type':     'post-market',
                'after_hours_timestamp': _dt.now().isoformat(),
            }

        # Check pre-market
        pre = fi.get('preMarketPrice')
        if pre and prev_close:
            chg_pct = round((pre - prev_close) / prev_close * 100, 3)
            return {
                'after_hours_price':     round(float(pre), 4),
                'after_hours_chg_pct':  chg_pct,
                'after_hours_type':     'pre-market',
                'after_hours_timestamp': _dt.now().isoformat(),
            }

        # Market closed fallback: use prepost=True history to get latest tick
        hist = t.history(period='1d', interval='5m', prepost=True)
        if hist is not None and not hist.empty:
            last = hist.iloc[-1]
            last_price = float(last['Close'])
            last_ts    = str(hist.index[-1])
            chg_pct = round((last_price - prev_close) / prev_close * 100, 3) if prev_close else None
            return {
                'after_hours_price':     round(last_price, 4),
                'after_hours_chg_pct':  chg_pct,
                'after_hours_type':     'last-tick',
                'after_hours_timestamp': last_ts,
            }

        return empty
    except Exception as e:
        logger.debug(f"get_after_hours_price({ticker}): {e}")
        return empty


# ── 6c. GLD / Gold fund-flow proxy ────────────────────────────────────────────

def get_gold_fund_flow() -> dict:
    """
    Approximate GLD ETF fund flows using daily volume × price as a proxy for
    daily dollar flow, and compute 5-day trend vs the prior 5-day average.
    Also pulls GLD AUM and shares outstanding for context.
    Added to resolve recurring 'gld fund flow data (etf inflows/outflows)' gap.
    """
    empty = {'gld_price': None, 'gld_volume': None, 'gld_dollar_flow_5d_avg': None,
             'gld_flow_trend': None, 'gld_aum_billions': None, 'gld_flow_note': None}
    try:
        import yfinance as yf
        import numpy as np
        t = yf.Ticker('GLD')
        hist = t.history(period='14d', interval='1d')
        if hist is None or hist.empty:
            return empty

        hist = hist.dropna(subset=['Close', 'Volume'])
        if len(hist) < 2:
            return empty

        # Dollar flow proxy = close × volume
        dollar_flow = hist['Close'] * hist['Volume']

        recent_5d = dollar_flow.iloc[-5:].mean()  if len(dollar_flow) >= 5 else dollar_flow.mean()
        prior_5d  = dollar_flow.iloc[-10:-5].mean() if len(dollar_flow) >= 10 else dollar_flow.iloc[:-5].mean() if len(dollar_flow) > 5 else None

        flow_trend = None
        if prior_5d and prior_5d > 0:
            flow_trend = round(float((recent_5d / prior_5d - 1) * 100), 2)  # % change vs prior window

        info = t.info
        total_assets = info.get('totalAssets')
        aum_billions = round(total_assets / 1e9, 2) if total_assets else None

        last_row = hist.iloc[-1]
        return {
            'gld_price':              round(float(last_row['Close']), 4),
            'gld_volume':             int(last_row['Volume']),
            'gld_dollar_flow_5d_avg': round(float(recent_5d) / 1e6, 2),  # in $M
            'gld_flow_trend_pct':     flow_trend,       # +% = accelerating inflows
            'gld_aum_billions':       aum_billions,
            'gld_flow_note':          'proxy: volume×price; positive trend = inflows accelerating',
        }
    except Exception as e:
        logger.debug(f"get_gold_fund_flow(): {e}")
        return empty


# ── 6d. Central bank gold purchasing data (FRED + news proxy) ─────────────────

def get_central_bank_gold_data() -> dict:
    """
    Fetch gold-related FRED macro indicators and GLD/XAUUSD context as a proxy
    for central bank gold purchasing activity.
    FRED series:
      - GOLDAMGBD228NLBM: Gold Fixing Price (London AM, USD/troy oz)
    Also returns GLD AUM trend as a crude demand proxy.
    Added to resolve recurring 'recent central bank gold purchasing data' gap.
    """
    empty = {'gold_spot_price': None, 'gold_spot_date': None,
             'gold_30d_chg_pct': None, 'gold_cb_note': None}
    try:
        import yfinance as yf
        import requests as _req
        from pathlib import Path as _Path
        import json as _json

        # -- Gold spot price from yfinance (GC=F front-month futures or GLD proxy) --
        try:
            gc = yf.Ticker('GC=F')
            hist_gc = gc.history(period='35d', interval='1d').dropna(subset=['Close'])
            if not hist_gc.empty:
                latest_price  = float(hist_gc['Close'].iloc[-1])
                oldest_price  = float(hist_gc['Close'].iloc[0])
                chg_30d = round((latest_price / oldest_price - 1) * 100, 2)
                latest_date   = str(hist_gc.index[-1])[:10]
            else:
                raise ValueError('empty GC=F history')
        except Exception:
            # Fallback to GLD
            gld = yf.Ticker('GLD')
            hist_gld = gld.history(period='35d', interval='1d').dropna(subset=['Close'])
            latest_price  = float(hist_gld['Close'].iloc[-1]) if not hist_gld.empty else None
            oldest_price  = float(hist_gld['Close'].iloc[0]) if len(hist_gld) > 1 else None
            chg_30d = round((latest_price / oldest_price - 1) * 100, 2) if latest_price and oldest_price else None
            latest_date   = str(hist_gld.index[-1])[:10] if not hist_gld.empty else None

        # -- FRED: Gold Fixing Price (AM) as benchmark context --
        fred_gold_price = None
        fred_gold_date  = None
        try:
            cfg_path = _Path(__file__).parent / 'trading_data' / 'orchestrator_config.json'
            fred_key = None
            if cfg_path.exists():
                cfg = _json.loads(cfg_path.read_text())
                fred_key = cfg.get('fred_api_key') or cfg.get('FRED_API_KEY')
            if not fred_key:
                import os as _os
                fred_key = _os.environ.get('FRED_API_KEY')
            if fred_key:
                r = _req.get(
                    'https://api.stlouisfed.org/fred/series/observations',
                    params={'series_id': 'GOLDAMGBD228NLBM', 'api_key': fred_key,
                            'limit': 5, 'sort_order': 'desc', 'file_type': 'json'},
                    timeout=10
                )
                if r.status_code == 200:
                    obs = r.json().get('observations', [])
                    for o in obs:
                        if o.get('value') not in ('.', '', None):
                            fred_gold_price = round(float(o['value']), 2)
                            fred_gold_date  = o.get('date', '')
                            break
        except Exception:
            pass

        return {
            'gold_spot_price':      round(latest_price, 2) if latest_price else None,
            'gold_spot_date':       latest_date,
            'gold_30d_chg_pct':     chg_30d,
            'gold_fred_am_fix':     fred_gold_price,
            'gold_fred_date':       fred_gold_date,
            'gold_cb_note':         (
                'Spot via GC=F/GLD. Central bank flows not directly available via free APIs; '
                'use WGC reports or Bloomberg for precise CB purchasing data.'
            ),
        }
    except Exception as e:
        logger.debug(f"get_central_bank_gold_data(): {e}")
        return empty


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
        from trading_db import get_sqlite_conn
        since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        conn = get_sqlite_conn(str(DB_PATH))
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

    # ── 5. VIX + VIX term structure ──────────────────────────────────────
    result['vix_level'] = get_vix()
    try:
        from vix_term_structure import VIXTermStructure as _VTS
        _vts = _VTS().get_term_structure_data()
        if _vts and 'vix_spot' in _vts:
            result['vix_spot']       = _vts.get('vix_spot')
            result['vix_3m']         = _vts.get('vix_3m')
            result['vix_6m']         = _vts.get('vix_6m')
            result['vix_vxv_ratio']  = _vts.get('vix_vxv_ratio')
            result['vix_regime']     = _vts.get('regime')
            # Override vix_level with the live spot value if we got it
            if _vts.get('vix_spot'):
                result['vix_level'] = _vts['vix_spot']
    except Exception as _vte:
        logger.debug(f"vix_term_structure enrich: {_vte}")

    # ── 5b. XLE/SPY relative strength (energy sector vs broad market) ────
    # Resolves recurring 'energy sector relative strength (XLE vs SPY)' gap
    xle_rs = get_xle_spy_rs()
    if xle_rs:
        result.update(xle_rs)
        logger.debug(f"  📊 XLE/SPY RS: 1d={xle_rs.get('xle_spy_rs_1d'):+.2f}%, XLE={xle_rs.get('xle_pct_1d'):+.2f}%")

    # ── 5c. After-hours / extended price for SPY (macro context) ───────────
    # Resolves recurring 'live after-hours price action' gap
    ah = get_after_hours_price('SPY')
    result.update(ah)
    if ah.get('after_hours_price'):
        logger.debug(f"  🌙 SPY after-hours: ${ah['after_hours_price']} ({ah.get('after_hours_chg_pct',0):+.2f}%) [{ah.get('after_hours_type')}]")

    # ── 5d. GLD fund flow proxy ───────────────────────────────────────────
    # Resolves recurring 'gld fund flow data (etf inflows/outflows) unavailable' gap
    gld_flow = get_gold_fund_flow()
    result.update(gld_flow)
    if gld_flow.get('gld_price'):
        logger.debug(f"  💰 GLD: ${gld_flow['gld_price']} | flow trend: {gld_flow.get('gld_flow_trend_pct',0):+.1f}% | AUM: ${gld_flow.get('gld_aum_billions')}B")

    # ── 5e. Central bank gold data proxy ─────────────────────────────────
    # Resolves recurring 'recent central bank gold purchasing data' gap
    gold_cb = get_central_bank_gold_data()
    result.update(gold_cb)
    if gold_cb.get('gold_spot_price'):
        logger.debug(f"  🟡 Gold spot: ${gold_cb['gold_spot_price']} | 30d chg: {gold_cb.get('gold_30d_chg_pct',0):+.2f}%")

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
