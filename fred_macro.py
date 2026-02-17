#!/usr/bin/env python3
"""
FRED Macro Data Integration
Fetches key macroeconomic indicators from the Federal Reserve Economic Data API.

All series are publicly accessible with a free FRED API key.
API key: https://fred.stlouisfed.org/docs/api/api_key.html (free, instant)

Key series tracked:
  - T10Y2Y:  10-Year minus 2-Year Treasury spread (yield curve inversion)
  - FEDFUNDS: Effective Federal Funds Rate
  - UNRATE:   Unemployment Rate
  - M2SL:     M2 Money Supply (month-over-month change)
  - CPIAUCSL: CPI All Items (inflation)
  - VIXCLS:   CBOE VIX (cross-check against Alpha Vantage)
  - DGS10:    10-Year Treasury Constant Maturity Rate
  - DGS2:     2-Year Treasury Constant Maturity Rate
  - UMCSENT:  U of Michigan Consumer Sentiment
  - BAMLH0A0HYM2: High Yield OAS (credit stress indicator)

Macro score contribution to composite DEFCON signal:
  - Yield curve inverted (T10Y2Y < 0): BEARISH
  - Fed funds rate rising fast (>25bps per meeting): tightening
  - Unemployment rising (>0.3pp in 3 months): deteriorating labor
  - M2 contracting YoY: liquidity squeeze
  - Credit spreads widening (HY OAS > 400bps): stress
"""

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent.resolve()
DB_PATH = SCRIPT_DIR / 'trading_data' / 'trading_history.db'
CONFIG_PATH = SCRIPT_DIR / 'trading_data' / 'orchestrator_config.json'

FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"

# Core series to track with their interpretation
FRED_SERIES = {
    'T10Y2Y': {
        'description': '10Y-2Y Treasury Spread (Yield Curve)',
        'unit': 'percent',
        'bearish_threshold': 0,      # < 0 = inverted = recession signal
        'warning_threshold': 0.5,    # < 0.5 = flattening
        'direction': 'lower_is_worse'
    },
    'FEDFUNDS': {
        'description': 'Fed Funds Rate',
        'unit': 'percent',
        'bearish_threshold': None,   # Context dependent
        'warning_threshold': None,
        'direction': 'tracking_only'
    },
    'UNRATE': {
        'description': 'Unemployment Rate',
        'unit': 'percent',
        'bearish_threshold': 5.5,    # > 5.5% = elevated
        'warning_threshold': 4.5,
        'direction': 'higher_is_worse'
    },
    'CPIAUCSL': {
        'description': 'CPI Inflation (All Items)',
        'unit': 'index',
        'bearish_threshold': None,   # YoY change matters
        'warning_threshold': None,
        'direction': 'tracking_only'
    },
    'M2SL': {
        'description': 'M2 Money Supply',
        'unit': 'billions',
        'bearish_threshold': None,   # YoY contraction matters
        'warning_threshold': None,
        'direction': 'tracking_only'
    },
    'DGS10': {
        'description': '10-Year Treasury Rate',
        'unit': 'percent',
        'bearish_threshold': 5.0,    # > 5% historically pressures stocks
        'warning_threshold': 4.5,
        'direction': 'higher_is_worse'
    },
    'DGS2': {
        'description': '2-Year Treasury Rate',
        'unit': 'percent',
        'bearish_threshold': None,
        'warning_threshold': None,
        'direction': 'tracking_only'
    },
    'UMCSENT': {
        'description': 'Consumer Sentiment (U of Michigan)',
        'unit': 'index',
        'bearish_threshold': 65,     # < 65 = pessimistic
        'warning_threshold': 75,
        'direction': 'lower_is_worse'
    },
    'BAMLH0A0HYM2': {
        'description': 'High Yield OAS (Credit Stress)',
        'unit': 'percent',
        'bearish_threshold': 500,    # > 500bps = stress
        'warning_threshold': 350,    # > 350bps = elevated
        'direction': 'higher_is_worse'
    }
}


def _load_fred_api_key() -> Optional[str]:
    """Load FRED API key from config or environment"""
    import os
    # Check environment variable first
    env_key = os.environ.get('FRED_API_KEY')
    if env_key:
        return env_key

    # Check orchestrator config
    try:
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH) as f:
                config = json.load(f)
            key = config.get('fred_api_key') or config.get('FRED_API_KEY')
            if key:
                return key
    except Exception:
        pass

    # Check dedicated fred_config.json
    fred_config_path = SCRIPT_DIR / 'trading_data' / 'fred_config.json'
    try:
        if fred_config_path.exists():
            with open(fred_config_path) as f:
                fred_config = json.load(f)
            return fred_config.get('api_key')
    except Exception:
        pass

    return None


def _safe_get(url: str, params: dict, timeout: int = 15) -> Optional[dict]:
    """Safe HTTP GET"""
    try:
        resp = requests.get(url, params=params, timeout=timeout)
        if resp.status_code == 200:
            return resp.json()
        else:
            logger.debug(f"FRED HTTP {resp.status_code}")
            return None
    except Exception as e:
        logger.debug(f"FRED request failed: {e}")
        return None


class FREDMacroTracker:
    """Fetches and analyzes FRED macroeconomic data"""

    def __init__(self, api_key: str = None, db_path: str = None):
        self.api_key = api_key or _load_fred_api_key()
        self.db_path = db_path or str(DB_PATH)
        self._cache = {}
        self._cache_time = {}
        self._cache_ttl_minutes = 60  # FRED data updates infrequently

        if not self.api_key:
            logger.warning("⚠️  No FRED API key found. Set fred_api_key in orchestrator_config.json or FRED_API_KEY env var.")
            logger.warning("   Get a free key at: https://fred.stlouisfed.org/docs/api/api_key.html")

    def fetch_series(self, series_id: str, limit: int = 12,
                     observation_start: str = None) -> Optional[List[Dict]]:
        """
        Fetch recent observations for a FRED series.

        Returns list of {date, value} dicts, most recent last.
        Returns None if API key missing or request fails.
        """
        if not self.api_key:
            return None

        # Check cache
        cache_key = f"{series_id}_{limit}"
        if cache_key in self._cache:
            age_minutes = (time.time() - self._cache_time.get(cache_key, 0)) / 60
            if age_minutes < self._cache_ttl_minutes:
                return self._cache[cache_key]

        # Default to 6 months back
        if not observation_start:
            observation_start = (datetime.now() - timedelta(days=180)).strftime('%Y-%m-%d')

        params = {
            'series_id': series_id,
            'api_key': self.api_key,
            'file_type': 'json',
            'sort_order': 'desc',
            'limit': limit,
            'observation_start': observation_start
        }

        data = _safe_get(FRED_BASE_URL, params)
        if not data:
            return None

        observations = data.get('observations', [])
        result = []
        for obs in reversed(observations):  # Chronological order
            if obs.get('value', '.') == '.':
                continue  # FRED uses '.' for missing data
            try:
                result.append({
                    'date': obs['date'],
                    'value': float(obs['value'])
                })
            except (ValueError, KeyError):
                continue

        # Cache
        self._cache[cache_key] = result
        self._cache_time[cache_key] = time.time()

        return result

    def get_latest_value(self, series_id: str) -> Optional[Tuple[str, float]]:
        """Get most recent (date, value) for a series"""
        observations = self.fetch_series(series_id, limit=5)
        if observations:
            latest = observations[-1]
            return (latest['date'], latest['value'])
        return None

    def calculate_yoy_change(self, series_id: str) -> Optional[float]:
        """Calculate year-over-year percentage change"""
        try:
            # Get 13 months of data
            start = (datetime.now() - timedelta(days=400)).strftime('%Y-%m-%d')
            observations = self.fetch_series(series_id, limit=14, observation_start=start)
            if not observations or len(observations) < 2:
                return None

            latest = observations[-1]['value']
            # Find observation closest to 12 months ago
            one_year_ago = datetime.now() - timedelta(days=365)
            older = None
            for obs in observations:
                obs_date = datetime.strptime(obs['date'], '%Y-%m-%d')
                if obs_date <= one_year_ago:
                    older = obs['value']

            if older and older != 0:
                return ((latest - older) / abs(older)) * 100
            return None
        except Exception:
            return None

    def calculate_3m_change(self, series_id: str) -> Optional[float]:
        """Calculate 3-month absolute change"""
        try:
            observations = self.fetch_series(series_id, limit=6)
            if not observations or len(observations) < 4:
                return None
            latest = observations[-1]['value']
            three_months_ago = observations[-4]['value']
            return latest - three_months_ago
        except Exception:
            return None

    def run_full_analysis(self) -> Dict:
        """
        Fetch all key series and compute macro risk score.

        Returns comprehensive dict for orchestrator, Gemini context, and Slack.
        """
        if not self.api_key:
            return {
                'available': False,
                'reason': 'No FRED API key configured',
                'macro_score': 50,  # Neutral assumption
                'macro_signals': []
            }

        logger.info("📊 FRED Macro: fetching economic indicators...")

        data = {}
        signals = []
        score_adjustments = []

        # ---- 1. Yield Curve (T10Y2Y) ----
        yc = self.get_latest_value('T10Y2Y')
        dgs10 = self.get_latest_value('DGS10')
        dgs2 = self.get_latest_value('DGS2')

        if yc:
            yc_date, yc_value = yc
            data['yield_curve_spread'] = yc_value
            data['yield_curve_date'] = yc_date

            if yc_value < 0:
                signals.append({
                    'type': 'yield_curve_inverted',
                    'value': yc_value,
                    'severity': 'bearish',
                    'description': f'Yield curve inverted ({yc_value:+.2f}%) — recession risk elevated'
                })
                score_adjustments.append(-20)
            elif yc_value < 0.5:
                signals.append({
                    'type': 'yield_curve_flat',
                    'value': yc_value,
                    'severity': 'caution',
                    'description': f'Yield curve flat ({yc_value:+.2f}%) — slowing growth signal'
                })
                score_adjustments.append(-10)
            else:
                signals.append({
                    'type': 'yield_curve_normal',
                    'value': yc_value,
                    'severity': 'neutral',
                    'description': f'Yield curve normal ({yc_value:+.2f}%)'
                })
                score_adjustments.append(5)

        if dgs10:
            data['rate_10y'] = dgs10[1]
            data['rate_10y_date'] = dgs10[0]
        if dgs2:
            data['rate_2y'] = dgs2[1]
            data['rate_2y_date'] = dgs2[0]

        # ---- 2. Fed Funds Rate ----
        ff = self.get_latest_value('FEDFUNDS')
        ff_3m_change = self.calculate_3m_change('FEDFUNDS')

        if ff:
            data['fed_funds_rate'] = ff[1]
            data['fed_funds_date'] = ff[0]
            data['fed_funds_3m_change'] = ff_3m_change

            if ff_3m_change and ff_3m_change > 0.5:
                signals.append({
                    'type': 'fed_tightening',
                    'value': ff_3m_change,
                    'severity': 'bearish',
                    'description': f'Fed tightening fast (+{ff_3m_change:.2f}% in 3mo) — liquidity squeeze risk'
                })
                score_adjustments.append(-15)
            elif ff_3m_change and ff_3m_change < -0.25:
                signals.append({
                    'type': 'fed_easing',
                    'value': ff_3m_change,
                    'severity': 'bullish',
                    'description': f'Fed easing ({ff_3m_change:.2f}% in 3mo) — supportive for risk assets'
                })
                score_adjustments.append(10)

        # ---- 3. Unemployment ----
        unrate = self.get_latest_value('UNRATE')
        unrate_3m = self.calculate_3m_change('UNRATE')

        if unrate:
            data['unemployment_rate'] = unrate[1]
            data['unemployment_date'] = unrate[0]
            data['unemployment_3m_change'] = unrate_3m

            if unrate_3m and unrate_3m > 0.3:
                signals.append({
                    'type': 'unemployment_rising',
                    'value': unrate[1],
                    'severity': 'bearish',
                    'description': f'Unemployment rising (+{unrate_3m:.1f}pp in 3mo to {unrate[1]:.1f}%) — Sahm Rule proximity'
                })
                score_adjustments.append(-15)
            elif unrate[1] > 5.5:
                signals.append({
                    'type': 'unemployment_elevated',
                    'value': unrate[1],
                    'severity': 'caution',
                    'description': f'Unemployment elevated ({unrate[1]:.1f}%)'
                })
                score_adjustments.append(-8)
            elif unrate[1] < 4.0:
                signals.append({
                    'type': 'unemployment_low',
                    'value': unrate[1],
                    'severity': 'bullish',
                    'description': f'Unemployment low ({unrate[1]:.1f}%) — strong labor market'
                })
                score_adjustments.append(5)

        # ---- 4. M2 Money Supply (YoY) ----
        m2_yoy = self.calculate_yoy_change('M2SL')
        m2_latest = self.get_latest_value('M2SL')

        if m2_latest:
            data['m2_billions'] = m2_latest[1]
            data['m2_yoy_change_pct'] = m2_yoy
            data['m2_date'] = m2_latest[0]

            if m2_yoy is not None:
                if m2_yoy < -2:
                    signals.append({
                        'type': 'm2_contracting',
                        'value': m2_yoy,
                        'severity': 'bearish',
                        'description': f'M2 contracting ({m2_yoy:.1f}% YoY) — liquidity draining from system'
                    })
                    score_adjustments.append(-12)
                elif m2_yoy > 8:
                    signals.append({
                        'type': 'm2_expanding_fast',
                        'value': m2_yoy,
                        'severity': 'caution',
                        'description': f'M2 expanding rapidly (+{m2_yoy:.1f}% YoY) — inflationary pressure'
                    })
                    score_adjustments.append(-5)

        # ---- 5. High Yield Credit Spreads ----
        hy_oas = self.get_latest_value('BAMLH0A0HYM2')
        hy_3m = self.calculate_3m_change('BAMLH0A0HYM2')

        if hy_oas:
            data['hy_oas_bps'] = hy_oas[1] * 100  # Convert to basis points
            data['hy_oas_date'] = hy_oas[0]
            data['hy_oas_3m_change'] = hy_3m

            hy_bps = hy_oas[1] * 100
            if hy_bps > 500:
                signals.append({
                    'type': 'credit_stress_extreme',
                    'value': hy_bps,
                    'severity': 'bearish',
                    'description': f'HY credit spreads extreme ({hy_bps:.0f}bps) — credit crisis risk'
                })
                score_adjustments.append(-25)
            elif hy_bps > 350:
                signals.append({
                    'type': 'credit_stress_elevated',
                    'value': hy_bps,
                    'severity': 'caution',
                    'description': f'HY credit spreads elevated ({hy_bps:.0f}bps) — financial stress'
                })
                score_adjustments.append(-12)
            elif hy_bps < 250:
                signals.append({
                    'type': 'credit_spreads_tight',
                    'value': hy_bps,
                    'severity': 'bullish',
                    'description': f'HY credit spreads tight ({hy_bps:.0f}bps) — risk appetite healthy'
                })
                score_adjustments.append(8)

        # ---- 6. Consumer Sentiment ----
        sentiment = self.get_latest_value('UMCSENT')
        if sentiment:
            data['consumer_sentiment'] = sentiment[1]
            data['consumer_sentiment_date'] = sentiment[0]

            if sentiment[1] < 65:
                signals.append({
                    'type': 'consumer_pessimistic',
                    'value': sentiment[1],
                    'severity': 'bearish',
                    'description': f'Consumer sentiment low ({sentiment[1]:.1f}) — demand slowdown risk'
                })
                score_adjustments.append(-8)
            elif sentiment[1] > 80:
                signals.append({
                    'type': 'consumer_optimistic',
                    'value': sentiment[1],
                    'severity': 'bullish',
                    'description': f'Consumer sentiment strong ({sentiment[1]:.1f})'
                })
                score_adjustments.append(5)

        # ---- Compute composite macro score (50 = neutral baseline) ----
        macro_score = 50 + sum(score_adjustments)
        macro_score = max(0, min(100, macro_score))  # Clamp 0-100

        # DEFCON contribution: convert macro score to DEFCON modifier
        # macro_score < 30 = bearish macro (push DEFCON down by 1)
        # macro_score 30-70 = neutral macro (no change)
        # macro_score > 70 = bullish macro (supportive)
        if macro_score < 30:
            defcon_modifier = -1  # Bearish: push DEFCON toward escalation
        elif macro_score < 40:
            defcon_modifier = -0.5
        elif macro_score > 70:
            defcon_modifier = 0.5  # Bullish: slight deescalation bias
        else:
            defcon_modifier = 0

        result = {
            'available': True,
            'macro_score': macro_score,
            'defcon_modifier': defcon_modifier,
            'macro_signals': signals,
            'data': data,
            'bearish_count': sum(1 for s in signals if s['severity'] == 'bearish'),
            'bullish_count': sum(1 for s in signals if s['severity'] == 'bullish'),
            'caution_count': sum(1 for s in signals if s['severity'] == 'caution'),
            'scan_timestamp': datetime.now().isoformat()
        }

        logger.info(f"  📊 Macro Score: {macro_score:.0f}/100 | DEFCON modifier: {defcon_modifier:+.1f}")
        logger.info(f"  📊 Signals: {result['bearish_count']} bearish, {result['caution_count']} caution, {result['bullish_count']} bullish")

        return result

    def save_to_db(self, macro_data: Dict):
        """Save macro indicators to database"""
        if not macro_data.get('available'):
            return

        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.cursor()

        d = macro_data.get('data', {})
        try:
            cursor.execute('''
                INSERT INTO macro_indicators
                (yield_curve_spread, fed_funds_rate, unemployment_rate,
                 m2_yoy_change, hy_oas_bps, consumer_sentiment,
                 rate_10y, rate_2y, macro_score, defcon_modifier,
                 bearish_signals, bullish_signals, signals_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                d.get('yield_curve_spread'),
                d.get('fed_funds_rate'),
                d.get('unemployment_rate'),
                d.get('m2_yoy_change_pct'),
                d.get('hy_oas_bps'),
                d.get('consumer_sentiment'),
                d.get('rate_10y'),
                d.get('rate_2y'),
                macro_data.get('macro_score', 50),
                macro_data.get('defcon_modifier', 0),
                macro_data.get('bearish_count', 0),
                macro_data.get('bullish_count', 0),
                json.dumps(macro_data.get('macro_signals', []))
            ))
            conn.commit()
        except Exception as e:
            logger.warning(f"Macro DB save failed: {e}")
        finally:
            conn.close()

    def get_latest_from_db(self) -> Optional[Dict]:
        """Get most recent macro data from DB for Gemini context"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM macro_indicators
                ORDER BY created_at DESC
                LIMIT 1
            ''')
            row = cursor.fetchone()
            conn.close()

            if row:
                return dict(row)
            return None
        except Exception:
            return None


# Standalone test
if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    print("\n📊 FRED Macro Tracker Test\n" + "="*60)

    tracker = FREDMacroTracker()

    if not tracker.api_key:
        print("\n❌ No FRED API key found!")
        print("   Get a free key at: https://fred.stlouisfed.org/docs/api/api_key.html")
        print("   Then add to trading_data/orchestrator_config.json:")
        print('   {"fred_api_key": "your_key_here", ...}')
        sys.exit(1)

    result = tracker.run_full_analysis()

    print(f"\n📊 Macro Score: {result['macro_score']:.0f}/100")
    print(f"📊 DEFCON Modifier: {result['defcon_modifier']:+.1f}")

    if result.get('data'):
        d = result['data']
        print(f"\n📈 Key Indicators:")
        if 'yield_curve_spread' in d:
            print(f"  Yield Curve (10Y-2Y): {d['yield_curve_spread']:+.2f}%")
        if 'fed_funds_rate' in d:
            print(f"  Fed Funds Rate: {d['fed_funds_rate']:.2f}%")
        if 'unemployment_rate' in d:
            print(f"  Unemployment: {d['unemployment_rate']:.1f}%")
        if 'm2_yoy_change_pct' in d and d['m2_yoy_change_pct'] is not None:
            print(f"  M2 YoY: {d['m2_yoy_change_pct']:+.1f}%")
        if 'hy_oas_bps' in d:
            print(f"  HY Credit Spreads: {d['hy_oas_bps']:.0f}bps")
        if 'consumer_sentiment' in d:
            print(f"  Consumer Sentiment: {d['consumer_sentiment']:.1f}")

    if result['macro_signals']:
        print(f"\n🚦 Macro Signals:")
        for sig in result['macro_signals']:
            emoji = '🔴' if sig['severity'] == 'bearish' else '🟡' if sig['severity'] == 'caution' else '🟢'
            print(f"  {emoji} {sig['description']}")
