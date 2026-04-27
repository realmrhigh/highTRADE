#!/usr/bin/env python3
"""
sector_rotation.py — Real-time sector rotation analysis for HighTrade.
Primary data source: Unusual Whales API (sector-etfs + market-tide endpoints).
"""

import logging
import os
import requests
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Unusual Whales API config ─────────────────────────────────────────────────
_UW_SECTOR_ETFS_URL = "https://api.unusualwhales.com/api/market/sector-etfs"
_UW_MARKET_TIDE_URL = "https://api.unusualwhales.com/api/market/market-tide"
_UW_CLIENT_ID = "100001"
_UW_CREDS_FILE = Path.home() / ".openclaw" / "creds" / "unusualwhales.env"


def _load_uw_api_key() -> Optional[str]:
    """Load UW API key from env or ~/.openclaw/creds/unusualwhales.env."""
    key = os.environ.get("UW_API_KEY")
    if key:
        return key.strip()
    if _UW_CREDS_FILE.exists():
        for line in _UW_CREDS_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith("UW_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _uw_headers(api_key: str) -> Dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "UW-CLIENT-API-ID": _UW_CLIENT_ID,
        "Accept": "application/json",
    }


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

    # ── Public entry point ────────────────────────────────────────────────────

    def get_rotation_data(self) -> Dict:
        """Fetch latest sector performance data.

        Tries Unusual Whales API first (faster, no timeout issues).
        Falls back to yfinance 3-month download on failure.
        """
        logger.info("📊 Fetching sector rotation data (Unusual Whales)...")
        result = self._fetch_uw_rotation_data()
        if result:
            logger.info(f"  ✅ UW sector data: {len(result.get('sectors', []))} sectors loaded")
            return result
        logger.warning("  ⚠️  UW fetch failed — falling back to yfinance")
        return self._get_rotation_data_yf()

    # ── Unusual Whales data source ────────────────────────────────────────────

    def _fetch_uw_rotation_data(self) -> Optional[Dict]:
        """Fetch sector rotation data from Unusual Whales API."""
        api_key = _load_uw_api_key()
        if not api_key:
            logger.warning("  ⚠️  UW_API_KEY not found in env or ~/.openclaw/creds/unusualwhales.env")
            return None

        headers = _uw_headers(api_key)

        # Fetch sector ETFs
        try:
            resp = requests.get(_UW_SECTOR_ETFS_URL, headers=headers, timeout=15)
            resp.raise_for_status()
            raw = resp.json()
        except requests.RequestException as e:
            logger.warning(f"  ⚠️  UW sector-etfs request failed: {e}")
            return None

        # Fetch market tide (optional — used for context, non-fatal on failure)
        tide_data = None
        try:
            tide_resp = requests.get(_UW_MARKET_TIDE_URL, headers=headers, timeout=10)
            tide_resp.raise_for_status()
            tide_data = tide_resp.json()
        except Exception as e:
            logger.debug(f"  UW market-tide fetch skipped: {e}")

        # Normalise response — UW may wrap in {"data": [...]} or return a list directly
        items = raw.get('data', raw) if isinstance(raw, dict) else raw
        if not items or not isinstance(items, (list, dict)):
            logger.warning("  ⚠️  UW sector-etfs returned unexpected format")
            return None
        if isinstance(items, dict):
            items = list(items.values())

        sector_results = []
        spy_1w: Optional[float] = None
        spy_1m: Optional[float] = None

        for item in items:
            if not isinstance(item, dict):
                continue
            ticker = (item.get('ticker') or item.get('symbol') or '').upper()
            if not ticker:
                continue

            # Accept multiple possible field names across UW API versions
            def _pct(item, *keys):
                for k in keys:
                    v = item.get(k)
                    if v is not None:
                        try:
                            f = float(v)
                            # Normalise ratio → percentage (e.g. 0.023 → 2.3%)
                            return f * 100 if abs(f) < 2.0 and f != 0 else f
                        except (TypeError, ValueError):
                            pass
                return 0.0

            perf_1w = _pct(item, 'week_change_percent', 'percent_change_1w',
                           'week_perf', 'weekly_return', 'change_1w')
            perf_1m = _pct(item, 'month_change_percent', 'percent_change_1m',
                           'month_perf', 'monthly_return', 'change_1m')
            price = 0.0
            for pk in ('close', 'price', 'last_price', 'current_price'):
                pv = item.get(pk)
                if pv:
                    try:
                        price = float(pv)
                        break
                    except (TypeError, ValueError):
                        pass

            if ticker == self.benchmark:
                spy_1w = perf_1w
                spy_1m = perf_1m
            elif ticker in self.sectors:
                sector_results.append({
                    'symbol': ticker,
                    'name': self.sectors[ticker],
                    'perf_1w': perf_1w,
                    'perf_1m': perf_1m,
                    'current_price': price,
                })

        if not sector_results:
            logger.warning("  ⚠️  UW sector-etfs: no matching sector ETFs found in response")
            return None

        bench_1w = spy_1w if spy_1w is not None else 0.0
        bench_1m = spy_1m if spy_1m is not None else 0.0
        for s in sector_results:
            s['rel_1w'] = s['perf_1w'] - bench_1w
            s['rel_1m'] = s['perf_1m'] - bench_1m
        sector_results.sort(key=lambda x: x['rel_1w'], reverse=True)

        result: Dict = {
            'timestamp': datetime.now().isoformat(),
            'benchmark_perf': {'1w': bench_1w, '1m': bench_1m},
            'sectors': sector_results,
            'top_sector_1w': sector_results[0]['name'] if sector_results else None,
            'bottom_sector_1w': sector_results[-1]['name'] if sector_results else None,
            'source': 'unusual_whales',
        }
        if tide_data:
            result['market_tide'] = tide_data
        return result

    # ── yfinance fallback (disabled — UW is primary) ──────────────────────────

    def _get_rotation_data_yf(self) -> Dict:
        """Disabled — yfinance removed. UW is the only sector data source."""
        logger.warning("  ⚠️  yfinance fallback disabled — UW unavailable, returning empty")
        return {}

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
