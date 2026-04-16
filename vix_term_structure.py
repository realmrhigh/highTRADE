#!/usr/bin/env python3
"""
vix_term_structure.py — Analysis of VIX futures and term structure for HighTrade.
Monitors spot VIX vs 3-month (VXV) and 6-month (VXMT) to detect shifts in market regime.
"""

import logging
import signal
import yfinance as yf
from datetime import datetime
from typing import Dict, Optional

logger = logging.getLogger(__name__)

class _YFTimeoutError(Exception):
    pass

def _yf_alarm_handler(signum, frame):
    raise _YFTimeoutError("yfinance download timed out")

# VIX: CBOE Volatility Index (Spot)
# VIX3M: CBOE 3-Month Volatility Index
# VIX6M: CBOE 6-Month Volatility Index
VOLATILITY_TICKERS = {
    '^VIX': 'VIX Spot',
    '^VIX3M': 'VIX 3-Month',
    '^VIX6M': 'VIX 6-Month'
}

class VIXTermStructure:
    """Analyzes the relationship between different volatility durations."""

    def __init__(self):
        self.tickers = list(VOLATILITY_TICKERS.keys())

    def _fetch_ticker_last(self, ticker: str) -> Optional[float]:
        """Fetch last close for a single ticker with robust fallback."""
        try:
            data = yf.download(ticker, period='5d', interval='1d', progress=False, timeout=15)
            if data is None or data.empty:
                return None
            # Single-ticker download returns a flat DataFrame with 'Close' as a column
            if 'Close' in data.columns:
                s = data['Close'].dropna()
                return float(s.iloc[-1]) if not s.empty else None
            # Multi-level fallback
            if hasattr(data.columns, 'levels'):
                try:
                    s = data['Close'][ticker].dropna()
                    return float(s.iloc[-1]) if not s.empty else None
                except (KeyError, TypeError):
                    pass
            return None
        except Exception as e:
            logger.warning(f"  ⚠️  Single-ticker fetch failed for {ticker}: {e}")
            return None

    def get_term_structure_data(self) -> Dict:
        """Fetch latest volatility data and calculate ratios."""
        logger.info("📊 Fetching VIX term structure data...")

        _old_handler = signal.signal(signal.SIGALRM, _yf_alarm_handler)
        signal.alarm(60)  # 60-second hard timeout for yfinance
        try:
            raw = yf.download(self.tickers, period='5d', interval='1d', progress=False, timeout=20)
            # yf.download can return None or raise if sqlite cache is unavailable
            if raw is None or raw.empty:
                raise ValueError("yf.download returned empty/None")
            data = raw.get('Close', raw.get('Adj Close'))
            if data is None or (hasattr(data, 'empty') and data.empty):
                raise ValueError("Close column missing from download result")

            def _last(col):
                if col not in data.columns:
                    return None
                s = data[col].dropna()
                return float(s.iloc[-1]) if not s.empty else None

            vix  = _last('^VIX')
            vxv  = _last('^VIX3M')
            vxmt = _last('^VIX6M')

            # Fall back to individual fetches if batch had NaN
            if vix  is None: vix  = self._fetch_ticker_last('^VIX')
            if vxv  is None: vxv  = self._fetch_ticker_last('^VIX3M')
            if vxmt is None: vxmt = self._fetch_ticker_last('^VIX6M')

            if vix is None or vxv is None or vxmt is None:
                logger.error("  ❌ Failed to fetch volatility data (some tickers returned None)")
                return {}

            # Satisfy original float cast path (already done above)
            vix  = float(vix)
            vxv  = float(vxv)
            vxmt = float(vxmt)

            # Ratios
            # VIX/VXV > 1 indicates backwardation (near-term fear > long-term fear) - BEARISH
            # VIX/VXV < 1 indicates contango (normal regime) - BULLISH/STABLE
            ratio_3m = vix / vxv
            ratio_6m = vix / vxmt

            # Regime assessment
            if ratio_3m > 1.05:
                regime = "CRITICAL BACKWARDATION"
                color = "RED"
            elif ratio_3m > 1.0:
                regime = "BACKWARDATION"
                color = "ORANGE"
            elif ratio_3m < 0.9:
                regime = "DEEP CONTANGO"
                color = "GREEN"
            else:
                regime = "NORMAL CONTANGO"
                color = "BLUE"

            result_dict = {
                'timestamp': datetime.now().isoformat(),
                'vix_spot': vix,
                'vix_3m': vxv,
                'vix_6m': vxmt,
                'vix_vxv_ratio': ratio_3m,
                'vix_vxmt_ratio': ratio_6m,
                'regime': regime,
                'regime_color': color
            }

            # Opportunistically enrich with after-hours + gold data
            # so market_context_block gets full context in AI prompts
            try:
                from data_bridge import get_after_hours_price, get_gold_fund_flow, get_central_bank_gold_data
                ah = get_after_hours_price('SPY')
                result_dict.update({
                    'after_hours_price':    ah.get('after_hours_price'),
                    'after_hours_chg_pct':  ah.get('after_hours_chg_pct'),
                    'after_hours_type':     ah.get('after_hours_type'),
                })
                gld = get_gold_fund_flow()
                result_dict.update({
                    'gld_price':           gld.get('gld_price'),
                    'gld_flow_trend_pct':  gld.get('gld_flow_trend_pct'),
                    'gld_aum_billions':    gld.get('gld_aum_billions'),
                })
                cb = get_central_bank_gold_data()
                result_dict.update({
                    'gold_spot_price':  cb.get('gold_spot_price'),
                    'gold_30d_chg_pct': cb.get('gold_30d_chg_pct'),
                    'gold_fred_am_fix': cb.get('gold_fred_am_fix'),
                })
            except Exception as _enrich_err:
                logger.debug(f"  VIX enrich: {_enrich_err}")

            return result_dict

        except _YFTimeoutError:
            logger.error("  ❌ VIX term structure fetch timed out after 60s — skipping")
            return {}
        except Exception as e:
            logger.error(f"  ❌ VIX term structure analysis failed: {e}")
            return {}
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, _old_handler)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    vix_struct = VIXTermStructure()
    result = vix_struct.get_term_structure_data()
    
    if result:
        print(f"\nVIX Term Structure Report ({result['timestamp']})")
        print(f"VIX Spot      : {result['vix_spot']:.2f}")
        print(f"VIX 3-Month   : {result['vix_3m']:.2f}")
        print(f"VIX 6-Month   : {result['vix_6m']:.2f}")
        print(f"VIX/VXV Ratio : {result['vix_vxv_ratio']:.2f}")
        print("-" * 50)
        print(f"Regime Assessment: {result['regime']}")
