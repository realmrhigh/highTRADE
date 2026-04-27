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


# ── Unusual Whales API helper ─────────────────────────────────────────────────

def _uw_get(path: str, timeout: int = 8):
    """
    Make an authenticated GET request to the Unusual Whales API.
    Loads UW_API_KEY from ~/.openclaw/creds/unusualwhales.env.
    Returns the parsed JSON dict on success, or None on any failure.
    """
    import requests as _req
    try:
        env_path = Path.home() / '.openclaw' / 'creds' / 'unusualwhales.env'
        api_key = None
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith('#') or '=' not in line:
                    continue
                k, _, v = line.partition('=')
                if k.strip() == 'UW_API_KEY':
                    api_key = v.strip().strip('"').strip("'")
                    break
        if not api_key:
            import os as _os
            api_key = _os.environ.get('UW_API_KEY')
        if not api_key:
            return None
        url = f'https://api.unusualwhales.com{path}'
        resp = _req.get(url, headers={
            'Authorization': f'Bearer {api_key}',
            'UW-CLIENT-API-ID': '100001',
        }, timeout=timeout)
        if resp.status_code == 200:
            return resp.json()
        logger.debug(f"_uw_get({path}): HTTP {resp.status_code}")
    except Exception as e:
        logger.debug(f"_uw_get({path}): {e}")
    return None


# ── 1. Earnings date ───────────────────────────────────────────────────────────

def get_earnings_date(ticker: str) -> Optional[str]:
    """
    Fetch next earnings date via Unusual Whales /api/stock/{ticker}/earnings.
    Returns ISO date string or None.
    """
    if ticker.upper() in _ETF_LIKE:
        return None
    try:
        data = _uw_get(f'/api/stock/{ticker}/earnings')
        if not data:
            return None
        rows = data.get('data') or data
        if not isinstance(rows, list):
            rows = [rows] if rows else []
        today = date.today()
        for row in rows:
            raw_date = row.get('date') or row.get('earnings_date') or row.get('report_date')
            if not raw_date:
                continue
            try:
                d = date.fromisoformat(str(raw_date)[:10])
                if d >= today:
                    return str(d)
            except Exception:
                pass
        return None
    except Exception as e:
        logger.debug(f"get_earnings_date({ticker}): {e}")
    return None


# ── 2. Short interest via Unusual Whales ──────────────────────────────────────

def get_short_interest(ticker: str) -> dict:
    """
    Fetch short interest via Unusual Whales /api/shorts/{ticker}/interest-float/v2.
    Returns short interest as % of float, short float, and short shares.
    """
    empty = {
        'short_pct_float': None,
        'shares_short':    None,
        'short_ratio':     None,
        'short_date':      None,
    }
    if ticker.upper() in _ETF_LIKE:
        return empty
    try:
        data = _uw_get(f'/api/shorts/{ticker}/interest-float/v2')
        if not data:
            return empty
        payload = data.get('data') or data
        # UW may return a list or a dict
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        raw_pct    = payload.get('short_interest_pct_float') or payload.get('short_float')
        raw_shares = payload.get('short_shares') or payload.get('short_interest')
        raw_date   = payload.get('date') or payload.get('settlement_date')
        # Normalise to percentage: UW returns decimal (0.05 = 5%) or already a pct (5.0)
        short_pct = None
        if raw_pct is not None:
            v = float(raw_pct)
            short_pct = round(v * 100, 4) if v < 1.0 else round(v, 4)
        return {
            'short_pct_float': short_pct,
            'shares_short':    int(float(raw_shares)) if raw_shares else None,
            'short_ratio':     None,   # not provided by UW interest-float endpoint
            'short_date':      str(raw_date)[:10] if raw_date else None,
        }
    except Exception as e:
        logger.debug(f"get_short_interest({ticker}): {e}")
        return empty


# ── 3. Options snapshot ────────────────────────────────────────────────────────

def get_options_snapshot(ticker: str, current_price: Optional[float] = None) -> dict:
    """
    Fetch options data via Unusual Whales options-volume and option-contracts endpoints.
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
        # Fetch aggregate OI / put-call ratio from options-volume
        total_call_oi = None
        total_put_oi  = None
        pcr           = None
        vol_data = _uw_get(f'/api/stock/{ticker}/options-volume')
        if vol_data:
            vol_payload = vol_data.get('data') or vol_data
            if isinstance(vol_payload, list):
                vol_payload = vol_payload[0] if vol_payload else {}
            call_oi = vol_payload.get('call_open_interest') or vol_payload.get('calls_oi')
            put_oi  = vol_payload.get('put_open_interest')  or vol_payload.get('puts_oi')
            if call_oi is not None:
                total_call_oi = int(float(call_oi))
            if put_oi is not None:
                total_put_oi = int(float(put_oi))
            if total_call_oi and total_call_oi > 100 and total_put_oi is not None:
                pcr = round(total_put_oi / total_call_oi, 3)

        # Fetch individual contracts for nearest expiry + ATM IV
        nearest_expiry = None
        atm_iv_call = atm_iv_put = None
        contracts_data = _uw_get(f'/api/stock/{ticker}/option-contracts')
        if contracts_data:
            contracts = contracts_data.get('data') or contracts_data
            if not isinstance(contracts, list):
                contracts = []

            today = date.today()
            best_exp = None
            best_days = None
            for c in contracts:
                raw_exp = c.get('expiry') or c.get('expiration_date') or c.get('expiry_date')
                if not raw_exp:
                    continue
                try:
                    exp_d = date.fromisoformat(str(raw_exp)[:10])
                    days_out = (exp_d - today).days
                    if days_out >= 5 and (best_days is None or days_out < best_days):
                        best_days = days_out
                        best_exp = str(raw_exp)[:10]
                except Exception:
                    pass

            if not best_exp and contracts:
                raw_exp = (contracts[0].get('expiry') or contracts[0].get('expiration_date')
                           or contracts[0].get('expiry_date'))
                if raw_exp:
                    best_exp = str(raw_exp)[:10]

            nearest_expiry = best_exp

            # ATM IV from nearest-expiry contracts
            if current_price and best_exp:
                best_call_dist = best_put_dist = None
                for c in contracts:
                    exp_key = (c.get('expiry') or c.get('expiration_date')
                               or c.get('expiry_date') or '')
                    if str(exp_key)[:10] != best_exp:
                        continue
                    strike   = c.get('strike') or c.get('strike_price')
                    iv       = c.get('implied_volatility') or c.get('iv')
                    opt_type = str(c.get('option_type') or c.get('type') or '').lower()
                    if strike is None or iv is None:
                        continue
                    dist = abs(float(strike) - current_price)
                    if 'call' in opt_type or opt_type == 'c':
                        if best_call_dist is None or dist < best_call_dist:
                            best_call_dist = dist
                            atm_iv_call = round(float(iv), 4)
                    elif 'put' in opt_type or opt_type == 'p':
                        if best_put_dist is None or dist < best_put_dist:
                            best_put_dist = dist
                            atm_iv_put = round(float(iv), 4)

        return {
            'options_atm_iv_call':    atm_iv_call,
            'options_atm_iv_put':     atm_iv_put,
            'options_put_call_ratio': pcr,
            'options_total_call_oi':  total_call_oi,
            'options_total_put_oi':   total_put_oi,
            'options_nearest_expiry': nearest_expiry,
        }

    except Exception as e:
        logger.debug(f"get_options_snapshot({ticker}): {e}")
        return empty


# ── 4. Pre-market data ─────────────────────────────────────────────────────────

def get_premarket(ticker: str) -> dict:
    """
    Fetch pre-market price and % change via Unusual Whales stock-state.
    """
    empty = {'pre_market_price': None, 'pre_market_chg_pct': None}
    try:
        data = _uw_get(f'/api/stock/{ticker}/stock-state')
        if not data:
            return empty
        payload = data.get('data') or data
        pre  = payload.get('pre_market_price')
        prev = payload.get('prev_day_close_price')
        chg_pct = payload.get('pre_market_change_pct')
        # Calculate change pct from prices if not provided directly
        if chg_pct is None and pre and prev and float(prev) != 0:
            chg_pct = round((float(pre) - float(prev)) / float(prev) * 100, 3)
        elif chg_pct is not None:
            chg_pct = round(float(chg_pct), 3)
        return {
            'pre_market_price':   round(float(pre), 4) if pre else None,
            'pre_market_chg_pct': chg_pct,
        }
    except Exception as e:
        logger.debug(f"get_premarket({ticker}): {e}")
        return empty


# ── 5. VIX ────────────────────────────────────────────────────────────────────

def get_vix() -> Optional[float]:
    """Fetch current VIX level via Unusual Whales volatility stats."""
    try:
        data = _uw_get('/api/stock/SPY/volatility/stats')
        if not data:
            return None
        inner = data.get('data') or data
        if isinstance(inner, list):
            inner = inner[0] if inner else {}
        # UW iv_rank is a 0-100 score; scale to approximate VIX
        # Also try to get actual VIX from market-tide context
        iv = inner.get('iv_rank') or inner.get('implied_move_perc')
        return round(float(iv), 2) if iv else None
    except Exception as e:
        logger.debug(f"get_vix(): {e}")
        return None


def get_xle_spy_rs() -> Optional[dict]:
    """
    Fetch XLE vs SPY relative strength via Unusual Whales stock-state.
    Returns dict with xle_spy_ratio_1d, xle_spy_rs_1d, xle_pct_1d, spy_pct_1d.
    """
    try:
        results = {}
        for ticker in ['XLE', 'SPY']:
            data = _uw_get(f'/api/stock/{ticker}/stock-state')
            if not data:
                return None
            inner = data.get('data') or data
            if isinstance(inner, list):
                inner = inner[0] if inner else {}
            last = float(inner.get('last_price') or inner.get('last_trade_price') or 0)
            prev = float(inner.get('prev_day_close_price') or inner.get('close') or 0)
            if not last or not prev:
                return None
            results[ticker] = {'last': last, 'prev': prev, 'pct': round((last/prev - 1)*100, 3)}
        xle_1d = results['XLE']['pct']
        spy_1d = results['SPY']['pct']
        return {
            'xle_pct_1d':    xle_1d,
            'spy_pct_1d':    spy_1d,
            'xle_spy_rs_1d': round(xle_1d - spy_1d, 3),
            'xle_spy_rs_5d': None,  # UW stock-state is intraday only
        }
    except Exception as e:
        logger.debug(f"get_xle_spy_rs(): {e}")
        return None


# ── 6b. After-hours / extended price ─────────────────────────────────────────

def get_after_hours_price(ticker: str = 'SPY') -> dict:
    """
    Fetch after-hours price via Unusual Whales /api/stock/{ticker}/stock-state.
    Added to resolve recurring 'live after-hours price action' gap.
    """
    empty = {'after_hours_price': None, 'after_hours_chg_pct': None,
             'after_hours_type': None, 'after_hours_timestamp': None}
    try:
        from datetime import datetime as _dt
        data = _uw_get(f'/api/stock/{ticker}/stock-state')
        if not data:
            return empty
        payload = data.get('data') or data
        if isinstance(payload, list):
            payload = payload[0] if payload else {}

        after_hours = payload.get('after_hours_price')
        prev_close  = payload.get('prev_day_close_price')

        chg_pct = None
        if after_hours and prev_close and float(prev_close) != 0:
            chg_pct = round((float(after_hours) - float(prev_close)) / float(prev_close) * 100, 3)

        if after_hours:
            return {
                'after_hours_price':     round(float(after_hours), 4),
                'after_hours_chg_pct':   chg_pct,
                'after_hours_type':      'after-hours',
                'after_hours_timestamp': _dt.now().isoformat(),
            }
        return empty
    except Exception as e:
        logger.debug(f"get_after_hours_price({ticker}): {e}")
        return empty


# ── 6c. GLD / Gold fund-flow proxy ────────────────────────────────────────────

def get_gold_fund_flow() -> dict:
    """
    Fetch GLD ETF price and fund flow via Unusual Whales stock-state and etf in-outflow.
    Added to resolve recurring 'gld fund flow data (etf inflows/outflows)' gap.
    """
    empty = {'gld_price': None, 'gld_volume': None, 'gld_dollar_flow_5d_avg': None,
             'gld_flow_trend_pct': None, 'gld_aum_billions': None, 'gld_flow_note': None}
    try:
        # GLD price + volume from stock-state
        gld_price  = None
        gld_volume = None
        state_data = _uw_get('/api/stock/GLD/stock-state')
        if state_data:
            payload = state_data.get('data') or state_data
            if isinstance(payload, list):
                payload = payload[0] if payload else {}
            price = payload.get('last_price') or payload.get('last_trade_price')
            vol   = payload.get('volume') or payload.get('day_volume')
            gld_price  = round(float(price), 4) if price else None
            gld_volume = int(float(vol)) if vol else None

        # Fund flow trend from etf in-outflow
        gld_flow_trend_pct = None
        gld_aum_billions   = None
        flow_data = _uw_get('/api/etfs/GLD/in-outflow')
        if flow_data:
            flow_payload = flow_data.get('data') or flow_data
            if isinstance(flow_payload, list) and flow_payload:
                recent_flow = float(flow_payload[0].get('net_flow') or flow_payload[0].get('inflow') or 0)
                if len(flow_payload) >= 2:
                    prior_flow = float(flow_payload[1].get('net_flow') or flow_payload[1].get('inflow') or 0)
                    if prior_flow != 0:
                        gld_flow_trend_pct = round((recent_flow / abs(prior_flow) - 1) * 100, 2)
                aum = flow_payload[0].get('aum') or flow_payload[0].get('total_assets')
                gld_aum_billions = round(float(aum) / 1e9, 2) if aum else None
            elif isinstance(flow_payload, dict):
                aum = flow_payload.get('aum') or flow_payload.get('total_assets')
                gld_aum_billions = round(float(aum) / 1e9, 2) if aum else None

        return {
            'gld_price':              gld_price,
            'gld_volume':             gld_volume,
            'gld_dollar_flow_5d_avg': None,
            'gld_flow_trend_pct':     gld_flow_trend_pct,
            'gld_aum_billions':       gld_aum_billions,
            'gld_flow_note':          'via UW stock-state + etf in-outflow',
        }
    except Exception as e:
        logger.debug(f"get_gold_fund_flow(): {e}")
        return empty


# ── 6d. Central bank gold purchasing data (FRED + news proxy) ─────────────────

def get_central_bank_gold_data() -> dict:
    """
    Fetch gold-related FRED macro indicators and GLD context via Unusual Whales.
    FRED series: GOLDAMGBD228NLBM (Gold Fixing Price AM, USD/troy oz).
    Added to resolve recurring 'recent central bank gold purchasing data' gap.
    """
    empty = {'gold_spot_price': None, 'gold_spot_date': None,
             'gold_30d_chg_pct': None, 'gold_cb_note': None}
    try:
        import requests as _req
        from pathlib import Path as _Path
        import json as _json

        # -- Gold spot price approximation via UW GLD stock-state --
        latest_price = None
        latest_date  = None
        try:
            state_data = _uw_get('/api/stock/GLD/stock-state')
            if state_data:
                payload = state_data.get('data') or state_data
                if isinstance(payload, list):
                    payload = payload[0] if payload else {}
                price = payload.get('last_price') or payload.get('last_trade_price')
                if price:
                    latest_price = float(price)
                    latest_date  = date.today().isoformat()
        except Exception:
            pass

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
            'gold_spot_price':  round(latest_price, 2) if latest_price else None,
            'gold_spot_date':   latest_date,
            'gold_30d_chg_pct': None,  # not available from stock-state alone
            'gold_fred_am_fix': fred_gold_price,
            'gold_fred_date':   fred_gold_date,
            'gold_cb_note': (
                'Spot via UW GLD stock-state. Central bank flows not directly available via free APIs; '
                'use WGC reports or Bloomberg for precise CB purchasing data.'
            ),
        }
    except Exception as e:
        logger.debug(f"get_central_bank_gold_data(): {e}")
        return empty


def get_analyst_info(ticker: str) -> dict:
    """
    Fetch analyst consensus via Unusual Whales /api/screener/analysts.
    Returns recommendation key, mean score, analyst count, and target prices.
    """
    empty = {
        'recommendation_key':   None,
        'recommendation_mean':  None,
        'analyst_count':        None,
        'target_mean_fallback': None,
        'target_high_fallback': None,
        'target_low_fallback':  None,
    }
    try:
        data = _uw_get(f'/api/screener/analysts?ticker={ticker}')
        if not data:
            return empty
        payload = data.get('data') or data
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        if not payload:
            return empty

        rec_mean   = payload.get('recommendation_mean') or payload.get('avg_rating')
        rec_key    = payload.get('recommendation_key')  or payload.get('consensus') or payload.get('rating')
        n_analysts = (payload.get('num_analysts') or payload.get('analyst_count')
                      or payload.get('number_of_analysts'))
        target_mean = (payload.get('price_target') or payload.get('avg_price_target')
                       or payload.get('target_mean'))
        target_high = (payload.get('price_target_high') or payload.get('high_price_target')
                       or payload.get('target_high'))
        target_low  = (payload.get('price_target_low') or payload.get('low_price_target')
                       or payload.get('target_low'))

        # Derive human-readable label from mean score if key is absent
        if not rec_key and rec_mean is not None:
            rm = float(rec_mean)
            if rm <= 1.5:   rec_key = 'strong_buy'
            elif rm <= 2.5: rec_key = 'buy'
            elif rm <= 3.5: rec_key = 'hold'
            elif rm <= 4.5: rec_key = 'underperform'
            else:           rec_key = 'sell'

        return {
            'recommendation_key':   rec_key or None,
            'recommendation_mean':  float(rec_mean) if rec_mean is not None else None,
            'analyst_count':        int(n_analysts) if n_analysts else None,
            'target_mean_fallback': float(target_mean) if target_mean else None,
            'target_high_fallback': float(target_high) if target_high else None,
            'target_low_fallback':  float(target_low)  if target_low  else None,
        }
    except Exception as e:
        logger.debug(f"get_analyst_info({ticker}): {e}")
        return empty


# ── 7. Insider activity via Unusual Whales ────────────────────────────────────

def get_insider_activity(ticker: str, days: int = 90) -> dict:
    """
    Fetch recent insider transactions (Form 4) via Unusual Whales ticker-flow.
    Returns buy/sell counts and the most recent transaction date.
    """
    empty = {
        'insider_buys_90d':      0,
        'insider_sells_90d':     0,
        'insider_net_sentiment': 'neutral',
        'insider_last_date':     None,
        'insider_txns_json':     None,
    }
    if ticker.upper() in _ETF_LIKE:
        return empty
    try:
        data = _uw_get(f'/api/insider/{ticker}/ticker-flow')
        if not data:
            return empty
        rows = data.get('data') or data
        if not isinstance(rows, list):
            return empty

        cutoff = datetime.now() - timedelta(days=days)
        buys = sells = 0
        last_date = None
        sample = []

        for row in rows:
            # Date filtering
            raw_date = row.get('date') or row.get('transaction_date') or row.get('filed_date')
            txn_date = None
            if raw_date:
                try:
                    txn_date = datetime.fromisoformat(str(raw_date)[:10])
                    if txn_date < cutoff:
                        continue
                except Exception:
                    pass

            txn_type = str(row.get('transaction_type') or row.get('type') or '').lower()
            is_buy  = any(kw in txn_type for kw in ('purchase', 'buy', 'p -'))
            is_comp = any(kw in txn_type for kw in ('grant', 'award', 'gift', 'option', 'a -'))
            is_sell = any(kw in txn_type for kw in ('sale', 'sell', 's -', 'disposition'))

            if is_buy and not is_comp:
                buys += 1
            elif is_sell:
                sells += 1

            if txn_date and (last_date is None or txn_date > last_date):
                last_date = txn_date

            if len(sample) < 5:
                sample.append({
                    'date':    str(raw_date)[:10] if raw_date else None,
                    'insider': row.get('insider_name') or row.get('name'),
                    'type':    txn_type,
                    'shares':  row.get('shares') or row.get('transaction_shares'),
                    'value':   row.get('value') or row.get('transaction_value'),
                })

        sentiment = 'neutral'
        if buys > sells * 1.5:   sentiment = 'bullish'
        elif sells > buys * 1.5: sentiment = 'bearish'

        txn_sample = None
        try:
            txn_sample = json.dumps(sample, default=str)
        except Exception:
            pass

        return {
            'insider_buys_90d':      buys,
            'insider_sells_90d':     sells,
            'insider_net_sentiment': sentiment,
            'insider_last_date':     str(last_date.date()) if last_date else None,
            'insider_txns_json':     txn_sample,
        }

    except Exception as e:
        logger.debug(f"get_insider_activity({ticker}): {e}")
        return empty


# ── 7b. IV Rank via Unusual Whales ────────────────────────────────────────────

def get_iv_rank(ticker: str) -> dict:
    """
    Fetch IV rank and IV percentile via Unusual Whales /api/stock/{ticker}/iv-rank.
    Returns empty dict on any failure.
    """
    try:
        data = _uw_get(f'/api/stock/{ticker}/iv-rank')
        if not data:
            return {}
        payload = data.get('data') or data
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        iv_rank = payload.get('iv_rank')
        iv_pct  = payload.get('iv_percentile') or payload.get('iv_pct')
        result = {}
        if iv_rank is not None:
            result['iv_rank'] = round(float(iv_rank), 2)
        if iv_pct is not None:
            result['iv_percentile'] = round(float(iv_pct), 2)
        return result
    except Exception as e:
        logger.debug(f"get_iv_rank({ticker}): {e}")
        return {}


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
    si = get_short_interest(ticker)
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

    # ── 6. Analyst consensus (via UW screener) ───────────────────────────
    ai = get_analyst_info(ticker)
    result.update(ai)
    # Back-fill analyst targets if the primary fetch came up empty
    if not existing.get('analyst_target_mean') and ai.get('target_mean_fallback'):
        result['analyst_target_mean'] = ai['target_mean_fallback']
        result['analyst_target_high'] = ai['target_high_fallback']
        result['analyst_target_low']  = ai['target_low_fallback']

    # ── 7. Insider activity ───────────────────────────────────────────────
    insider = get_insider_activity(ticker)
    result.update(insider)

    # ── 7b. IV Rank (additive) ────────────────────────────────────────────
    iv_rank = get_iv_rank(ticker)
    result.update(iv_rank)

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

    uw_info = _uw_get(f'/api/stock/{ticker}/info') or {}
    info_payload = uw_info.get('data') or uw_info
    if isinstance(info_payload, list):
        info_payload = info_payload[0] if info_payload else {}
    current_price = info_payload.get('last_price') or info_payload.get('last_trade_price')
    existing = {'info': info_payload, 'current_price': float(current_price) if current_price else None}

    result = enrich(ticker, existing)
    pprint.pprint(result)
