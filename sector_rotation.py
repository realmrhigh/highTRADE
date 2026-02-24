#!/usr/bin/env python3
"""
sector_rotation.py — Real-time sector rotation analysis for HighTrade.
Fetches major sector ETFs and calculates relative strength.
"""

import logging
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

SECTOR_ETFS = {
    'XLK': 'Technology',
    'XLE': 'Energy',
    'XLF': 'Financials',
    'XLV': 'Health Care',
    'XLY': 'Consumer Discretionary',
    'XLP': 'Consumer Staples',
    'XLI': 'Industrials',
    'XLB': 'Materials',
    'XLU': 'Utilities',
    'XLC': 'Communication Services',
    'XLRE': 'Real Estate'
}

BENCHMARK = 'SPY'

class SectorRotationAnalyzer:
    """Analyzes relative strength and rotation across equity sectors."""

    def __init__(self):
        self.sectors = SECTOR_ETFS
        self.benchmark = BENCHMARK

    def get_rotation_data(self) -> Dict:
        """Fetch latest performance data for all sectors."""
        logger.info("📊 Fetching sector rotation data...")
        
        symbols = list(self.sectors.keys()) + [self.benchmark]
        try:
            # Fetch last 3 months to get enough context for 1w/1m/3m changes
            data = yf.download(symbols, period='3mo', interval='1d', progress=False)['Close']
            
            if data.empty:
                logger.error("  ❌ Failed to fetch sector data from yfinance")
                return {}

            results = {}
            bench_perf = self._calculate_perf(data[self.benchmark])
            
            sector_results = []
            for sym, name in self.sectors.items():
                if sym not in data:
                    continue
                
                perf = self._calculate_perf(data[sym])
                rel_strength = {
                    'symbol': sym,
                    'name': name,
                    'perf_1w': perf['1w'],
                    'perf_1m': perf['1m'],
                    'rel_1w': perf['1w'] - bench_perf['1w'],
                    'rel_1m': perf['1m'] - bench_perf['1m'],
                    'current_price': float(data[sym].iloc[-1])
                }
                sector_results.append(rel_strength)

            # Sort by 1-week relative strength
            sector_results.sort(key=lambda x: x['rel_1w'], reverse=True)

            return {
                'timestamp': datetime.now().isoformat(),
                'benchmark_perf': bench_perf,
                'sectors': sector_results,
                'top_sector_1w': sector_results[0]['name'] if sector_results else None,
                'bottom_sector_1w': sector_results[-1]['name'] if sector_results else None
            }

        except Exception as e:
            logger.error(f"  ❌ Sector rotation analysis failed: {e}")
            return {}

    def _calculate_perf(self, series: pd.Series) -> Dict:
        """Calculate % change over various timeframes."""
        try:
            curr = series.iloc[-1]
            prev_1w = series.iloc[-6] if len(series) >= 6 else series.iloc[0]
            prev_1m = series.iloc[-21] if len(series) >= 21 else series.iloc[0]
            
            return {
                '1w': ((curr / prev_1w) - 1) * 100,
                '1m': ((curr / prev_1m) - 1) * 100
            }
        except Exception:
            return {'1w': 0, '1m': 0}

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    analyzer = SectorRotationAnalyzer()
    rotation = analyzer.get_rotation_data()
    
    if rotation:
        print(f"\nSector Rotation Report ({rotation['timestamp']})")
        print(f"Benchmark (SPY) 1W: {rotation['benchmark_perf']['1w']:.2f}%")
        print("-" * 50)
        print(f"{'Sector':<25} {'1W Perf':<10} {'1W Rel':<10}")
        for s in rotation['sectors']:
            print(f"{s['name']:<25} {s['perf_1w']:>8.2f}% {s['rel_1w']:>8.2f}%")
