#!/usr/bin/env python3
"""
vix_term_structure.py — Analysis of VIX futures and term structure for HighTrade.
Monitors spot VIX vs 3-month (VXV) and 6-month (VXMT) to detect shifts in market regime.
"""

import logging
import yfinance as yf
from datetime import datetime
from typing import Dict, Optional

logger = logging.getLogger(__name__)

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

    def get_term_structure_data(self) -> Dict:
        """Fetch latest volatility data and calculate ratios."""
        logger.info("📊 Fetching VIX term structure data...")
        
        try:
            data = yf.download(self.tickers, period='5d', interval='1d', progress=False)['Close']
            
            if data.empty:
                logger.error("  ❌ Failed to fetch volatility data")
                return {}

            # Get latest values
            vix = float(data['^VIX'].iloc[-1])
            vxv = float(data['^VIX3M'].iloc[-1])
            vxmt = float(data['^VIX6M'].iloc[-1])

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

            return {
                'timestamp': datetime.now().isoformat(),
                'vix_spot': vix,
                'vix_3m': vxv,
                'vix_6m': vxmt,
                'vix_vxv_ratio': ratio_3m,
                'vix_vxmt_ratio': ratio_6m,
                'regime': regime,
                'regime_color': color
            }

        except Exception as e:
            logger.error(f"  ❌ VIX term structure analysis failed: {e}")
            return {}

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
