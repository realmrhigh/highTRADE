#!/usr/bin/env python3
"""
sector_rotation.py — Real-time sector rotation analysis for HighTrade.
Fetches major sector ETFs and calculates relative strength.
"""

import logging
import signal
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

class _YFTimeoutError(Exception):
    pass

def _yf_alarm_handler(signum, frame):
    raise _YFTimeoutError("yfinance download timed out")

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

# Crisis-type → sector preferences
# Maps crisis regimes to which sectors to FAVOR, AVOID, and ROTATE TO on de-escalation
CRISIS_SECTOR_MAP = {
    'geopolitical_trade': {
        'favor': ['Energy', 'Industrials', 'Materials'],
        'avoid': ['Technology', 'Consumer Discretionary'],
        'deescalation_rotate_to': ['Technology', 'Consumer Discretionary', 'Communication Services'],
    },
    'inflation_rate': {
        'favor': ['Energy', 'Materials', 'Financials'],
        'avoid': ['Utilities', 'Real Estate'],
        'deescalation_rotate_to': ['Technology', 'Real Estate', 'Utilities'],
    },
    'tech_crash': {
        'favor': ['Consumer Staples', 'Utilities', 'Health Care'],
        'avoid': ['Technology', 'Communication Services'],
        'deescalation_rotate_to': ['Technology', 'Communication Services'],
    },
    'liquidity_credit': {
        'favor': ['Consumer Staples', 'Utilities', 'Health Care'],
        'avoid': ['Financials', 'Real Estate'],
        'deescalation_rotate_to': ['Financials', 'Technology'],
    },
    'pandemic_health': {
        'favor': ['Health Care', 'Technology', 'Communication Services'],
        'avoid': ['Industrials', 'Energy'],
        'deescalation_rotate_to': ['Industrials', 'Energy', 'Consumer Discretionary'],
    },
    'market_correction': {
        'favor': ['Consumer Staples', 'Utilities', 'Health Care'],
        'avoid': ['Consumer Discretionary', 'Technology'],
        'deescalation_rotate_to': ['Technology', 'Consumer Discretionary', 'Financials'],
    },
}


class SectorRotationAnalyzer:
    """Analyzes relative strength and rotation across equity sectors."""

    def __init__(self):
        self.sectors = SECTOR_ETFS
        self.benchmark = BENCHMARK

    def get_rotation_data(self) -> Dict:
        """Fetch latest performance data for all sectors."""
        logger.info("📊 Fetching sector rotation data...")

        symbols = list(self.sectors.keys()) + [self.benchmark]
        _old_handler = signal.signal(signal.SIGALRM, _yf_alarm_handler)
        signal.alarm(60)  # 60-second hard timeout for yfinance
        try:
            # Fetch last 3 months to get enough context for 1w/1m/3m changes
            raw = yf.download(symbols, period='3mo', interval='1d', progress=False, auto_adjust=True, timeout=20)

            # yfinance 1.2+ returns MultiIndex columns for multi-ticker downloads.
            # Extract 'Close' safely; fall back to per-ticker download for any missing/None columns.
            if raw.empty:
                logger.error("  ❌ Failed to fetch sector data from yfinance (empty response)")
                return {}

            if isinstance(raw.columns, pd.MultiIndex):
                # Standard multi-ticker result: columns are (field, ticker)
                if 'Close' in raw.columns.get_level_values(0):
                    data = raw['Close']
                else:
                    logger.error("  ❌ 'Close' level missing from MultiIndex columns")
                    return {}
            else:
                # Single-ticker fallback (shouldn't happen with multiple symbols)
                data = raw[['Close']] if 'Close' in raw.columns else raw

            # For any symbols that are all-NaN or missing, attempt individual download fallback
            missing_syms = [s for s in symbols if s not in data.columns or data[s].dropna().empty]
            if missing_syms:
                logger.warning(f"  ⚠️ Falling back to per-ticker download for: {missing_syms}")
                for sym in missing_syms:
                    try:
                        sym_raw = yf.download(sym, period='3mo', interval='1d', progress=False, auto_adjust=True)
                        if not sym_raw.empty and 'Close' in sym_raw.columns:
                            data[sym] = sym_raw['Close']
                    except Exception as sym_e:
                        logger.warning(f"  ⚠️ Per-ticker fallback failed for {sym}: {sym_e}")

            results = {}
            if self.benchmark not in data.columns or data[self.benchmark].dropna().empty:
                logger.error(f"  ❌ Benchmark {self.benchmark} data unavailable")
                return {}

            bench_perf = self._calculate_perf(data[self.benchmark])
            
            sector_results = []
            for sym, name in self.sectors.items():
                if sym not in data.columns or data[sym].dropna().empty:
                    logger.warning(f"  ⚠️ Skipping {sym} — no valid data")
                    continue
                
                perf = self._calculate_perf(data[sym])
                last_valid = data[sym].dropna().iloc[-1]
                rel_strength = {
                    'symbol': sym,
                    'name': name,
                    'perf_1w': perf['1w'],
                    'perf_1m': perf['1m'],
                    'rel_1w': perf['1w'] - bench_perf['1w'],
                    'rel_1m': perf['1m'] - bench_perf['1m'],
                    'current_price': float(last_valid)
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

        except _YFTimeoutError:
            logger.error("  ❌ Sector rotation fetch timed out after 60s — skipping")
            return {}
        except Exception as e:
            logger.error(f"  ❌ Sector rotation analysis failed: {e}")
            return {}
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, _old_handler)

    def get_sector_context(self, crisis_type: str, defcon_level: int,
                           is_winding_down: bool = False,
                           deescalation_score: float = 0.0) -> Dict:
        """
        Generate sector guidance based on current crisis type and DEFCON phase.

        Returns dict with favored_sectors, avoided_sectors, rotation_guidance (text),
        and phase description.
        """
        rotation_data = self.get_rotation_data()

        mapping = CRISIS_SECTOR_MAP.get(crisis_type, CRISIS_SECTOR_MAP['market_correction'])

        if is_winding_down or deescalation_score >= 40:
            # During wind-down or de-escalation: rotate toward growth/recovery sectors
            favored = mapping.get('deescalation_rotate_to', mapping['favor'])
            avoided = []  # Less restrictive during de-escalation
            phase = 'de-escalation / wind-down'
        elif defcon_level <= 2:
            # Deep crisis: favor defensive/crisis sectors
            favored = mapping['favor']
            avoided = mapping['avoid']
            phase = 'crisis (DEFCON 1-2)'
        elif defcon_level == 3:
            # Moderate stress: favor crisis sectors but less restrictive
            favored = mapping['favor']
            avoided = mapping['avoid'][:1]  # Only avoid the weakest
            phase = 'elevated (DEFCON 3)'
        else:
            # Peacetime: no sector bias, follow relative strength
            if rotation_data and rotation_data.get('sectors'):
                top_3 = [s['name'] for s in rotation_data['sectors'][:3]]
                favored = top_3
            else:
                favored = []
            avoided = []
            phase = 'peacetime (DEFCON 4-5)'

        # Cross-reference with actual relative strength data
        strong_sectors = []
        weak_sectors = []
        if rotation_data and rotation_data.get('sectors'):
            for s in rotation_data['sectors']:
                if s['rel_1w'] > 0.5:
                    strong_sectors.append(s['name'])
                elif s['rel_1w'] < -0.5:
                    weak_sectors.append(s['name'])

        # Build text guidance for prompt injection
        guidance_lines = [
            f"SECTOR ROTATION CONTEXT (phase: {phase}):",
            f"  Crisis type: {crisis_type}",
            f"  Favored sectors: {', '.join(favored) if favored else 'No specific bias'}",
            f"  Sectors to avoid: {', '.join(avoided) if avoided else 'None'}",
        ]
        if strong_sectors:
            guidance_lines.append(f"  Strong relative strength (1w): {', '.join(strong_sectors[:3])}")
        if weak_sectors:
            guidance_lines.append(f"  Weak relative strength (1w): {', '.join(weak_sectors[:3])}")
        if is_winding_down:
            guidance_lines.append(
                "  ** WIND-DOWN ACTIVE: Prefer rotating INTO growth/recovery sectors. "
                "Avoid initiating new defensive/crisis positions.**"
            )

        return {
            'favored_sectors': favored,
            'avoided_sectors': avoided,
            'strong_by_momentum': strong_sectors,
            'weak_by_momentum': weak_sectors,
            'phase': phase,
            'rotation_guidance': '\n'.join(guidance_lines),
            'sector_data': rotation_data,
        }

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
