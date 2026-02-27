#!/usr/bin/env python3
"""
HighTrade Signal Monitoring System
Continuously monitors real-time market signals and calculates DEFCON levels
Integrates with FRED API for bond yield data and news feeds for sentiment
"""

import sqlite3
import requests
import json
from datetime import datetime, timedelta
from pathlib import Path
import time

# Use SCRIPT_DIR to ensure we're in the correct project directory
SCRIPT_DIR = Path(__file__).parent.resolve()
DB_PATH = SCRIPT_DIR / 'trading_data' / 'trading_history.db'

# FRED API - Federal Reserve Economic Data (free tier)
FRED_API_KEY = "98ac4e761ff2e37793f310bcfb4f54c9"  # Get from https://fred.stlouisfed.org/docs/api/fred/
FRED_ENDPOINTS = {
    'DGS10': 'DGS10',      # 10-Year Treasury Constant Maturity
    'VIXCLS': 'VIXCLS',    # VIX
}

class SignalMonitor:
    """Main monitoring engine"""

    def __init__(self, db_path):
        self.db_path = db_path
        self.signal_scores = {}
        self.defcon_level = 5
        self.last_yield = 3.8
        self.last_vix = 18.5
        self.last_sp500 = 5000.0
        self.cycle_count = 0

    def connect(self):
        """Connect to database"""
        self.conn = sqlite3.connect(str(self.db_path))
        self.cursor = self.conn.cursor()

    def disconnect(self):
        """Disconnect from database"""
        self.conn.close()

    def fetch_bond_yield(self):
        """Fetch 10-year bond yield from FRED API"""
        try:
            url = (f"https://api.stlouisfed.org/fred/series/observations"
                   f"?series_id=DGS10&api_key={FRED_API_KEY}"
                   f"&file_type=json&sort_order=desc&limit=5")
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                data = response.json()
                observations = data.get('observations', [])
                # Skip entries with missing value '.'
                for obs in observations:
                    if obs['value'] != '.':
                        yield_value = float(obs['value'])
                        yield_date = obs['date']
                        return {'yield': yield_value, 'date': yield_date}
            else:
                print(f"  FRED API Error: {response.status_code} - {response.text[:100]}")
        except Exception as e:
            print(f"  FRED API Exception: {e}")
        return None

    def fetch_vix(self):
        """Fetch VIX from Yahoo Finance v8 chart API"""
        try:
            url = "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?interval=1d&range=1d"
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                price = data['chart']['result'][0]['meta']['regularMarketPrice']
                return {'vix': price, 'timestamp': datetime.now().isoformat()}
            else:
                print(f"  Yahoo VIX Error: {response.status_code}")
        except Exception as e:
            print(f"  Warning: Could not fetch VIX: {e}")
        return None

    def fetch_market_prices(self):
        """Fetch S&P 500 prices from Yahoo Finance v8 chart API"""
        try:
            url = "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?interval=1d&range=1d"
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                meta = data['chart']['result'][0]['meta']
                price = meta['regularMarketPrice']
                prev_close = meta['chartPreviousClose']
                change_pct = ((price - prev_close) / prev_close) * 100
                return {'sp500': price, 'change_pct': round(change_pct, 2)}
            else:
                print(f"  Yahoo S&P Error: {response.status_code}")
        except Exception as e:
            print(f"  Yahoo Finance Exception: {e}")
        return None

    def get_simulated_data(self):
        """Generate simulated market data for demonstration/fallback"""
        import random

        # Simulate realistic market movements
        self.cycle_count += 1

        # Slight random variations
        yield_change = random.uniform(-0.05, 0.05)
        vix_change = random.uniform(-1, 1)
        sp500_change = random.uniform(-0.3, 0.3)

        yield_value = max(2.5, min(5.5, self.last_yield + yield_change))
        vix_value = max(10, min(50, self.last_vix + vix_change))
        sp500_pct = sp500_change

        # Update last values for next cycle
        self.last_yield = yield_value
        self.last_vix = vix_value

        return {
            'yield_data': {'yield': round(yield_value, 2), 'date': datetime.now().strftime('%Y-%m-%d')},
            'vix_data': {'vix': round(vix_value, 2), 'timestamp': datetime.now().isoformat()},
            'market_data': {'sp500': 5000, 'change_pct': round(sp500_pct, 2)}
        }

    def calculate_signal_scores(self, yield_data, vix_data, market_data):
        """Calculate composite signal scores based on current conditions"""
        scores = {}

        # Bond yield spike detection (40+ bps in 3 days = alert)
        if yield_data and yield_data['yield'] > 4.0:
            # Simulating 3-day change; in production would query historical data
            scores['bond_yield_spike'] = min(100, (yield_data['yield'] - 3.5) * 10)
        else:
            scores['bond_yield_spike'] = 0

        # VIX spike detection (>25 is elevated, >40 is extreme)
        if vix_data and vix_data['vix'] > 25:
            scores['vix_spike'] = min(100, (vix_data['vix'] - 15) * 2)
        else:
            scores['vix_spike'] = 0

        # Market drawdown detection (>4% = crisis mode)
        if market_data and market_data['change_pct'] < -4:
            scores['market_drawdown'] = min(100, abs(market_data['change_pct']) * 5)
        else:
            scores['market_drawdown'] = 0

        self.signal_scores = scores
        return scores

    def calculate_defcon_level(self, signal_scores, market_data, news_signal=None,
                               flash_forecast=None, macro_modifier=None):
        """Determine DEFCON level based on signal clustering, news override, and Claude analysis"""
        composite_score = sum(signal_scores.values()) / len(signal_scores) if signal_scores else 0

        market_drop = market_data['change_pct'] if market_data else 0

        # Calculate base DEFCON from quantitative signals
        if composite_score >= 80 and market_drop < -4:
            base_defcon = 1  # EXECUTE
        elif composite_score >= 60 or market_drop < -4:
            base_defcon = 2  # PRE-BOTTOM
        elif composite_score >= 40 or market_drop < -2:
            base_defcon = 3  # CRISIS
        elif composite_score >= 20 or market_drop < -1:
            base_defcon = 4  # ELEVATED
        else:
            base_defcon = 5  # PEACETIME

        # ── Soft nudges from macro environment and flash DEFCON forecast ────────
        # Each source contributes at most ±1. Combined nudge is clamped to ±1.
        # DEFCON scale: 1=most bullish (buy/execute) → 5=most defensive (hold cash)
        _nudge = 0
        if macro_modifier is not None:
            # macro_modifier: negative = bullish conditions (lower DEFCON),
            #                 positive = stressed conditions (raise DEFCON)
            # Use threshold comparison — not round() — to avoid banker's rounding on -0.5
            if macro_modifier <= -0.5:
                _nudge -= 1   # macro says conditions are favorable
            elif macro_modifier >= 0.5:
                _nudge += 1   # macro says conditions are stressed
        if flash_forecast is not None:
            try:
                _ff = int(flash_forecast)
                if 1 <= _ff <= 5:
                    if _ff < base_defcon:
                        _nudge -= 1   # flash analysis more bullish than quant signals
                    elif _ff > base_defcon:
                        _nudge += 1   # flash analysis more bearish than quant signals
            except (TypeError, ValueError):
                pass
        _nudge = max(-1, min(1, _nudge))   # clamp combined nudge to ±1
        base_defcon = max(1, min(5, base_defcon + _nudge))
        if _nudge != 0:
            import logging as _logging
            _logging.getLogger(__name__).info(
                f"  💡 Soft nudge applied: {'+' if _nudge > 0 else ''}{_nudge} "
                f"(macro_modifier={macro_modifier}, flash_forecast={flash_forecast})"
                f" → base DEFCON {base_defcon}"
            )

        # Check for Claude analysis feedback (if available)
        if news_signal and news_signal.get('news_signal_id'):
            import logging
            logger = logging.getLogger(__name__)
            logger.info(f"🔍 Checking for Claude analysis on news_signal_id={news_signal.get('news_signal_id')}")
            claude_adjustment = self._check_claude_analysis(news_signal.get('news_signal_id'))

            if claude_adjustment:
                import logging
                logger = logging.getLogger(__name__)
                logger.info(f"📊 Claude Analysis Available:")
                logger.info(f"   Enhanced Confidence: {claude_adjustment['enhanced_confidence']}/100")
                logger.info(f"   Adjustment: {claude_adjustment['confidence_adjustment']:+.1f} points")
                logger.info(f"   Recommendation: {claude_adjustment['recommended_action']}")
                logger.info(f"   Reasoning: {claude_adjustment['reasoning'][:100]}...")

                # Apply Claude's confidence adjustment to news override logic
                if claude_adjustment['enhanced_confidence'] >= 85:
                    # Claude high confidence → Force DEFCON 2
                    logger.warning(f"🧠 CLAUDE OVERRIDE: DEFCON {base_defcon} → 2")
                    logger.info(f"   Reason: {claude_adjustment['reasoning']}")
                    self.defcon_level = 2
                    return self.defcon_level, composite_score

                elif claude_adjustment['confidence_adjustment'] < -20:
                    # Claude significantly lowered confidence → Cancel override
                    logger.info(f"🧠 CLAUDE CAUTION: Confidence lowered by {claude_adjustment['confidence_adjustment']:.1f}")
                    logger.info(f"   Canceling automated news override")
                    # Fall through to base_defcon calculation (no override)
                    self.defcon_level = base_defcon
                    return self.defcon_level, composite_score

        # Check for news override (if no Claude analysis or moderate adjustment)
        if news_signal and news_signal.get('breaking_news_override'):
            recommended_defcon = news_signal.get('recommended_defcon')
            if recommended_defcon and recommended_defcon < base_defcon:
                import logging
                logger = logging.getLogger(__name__)
                logger.warning(f"🚨 NEWS OVERRIDE: DEFCON {base_defcon} → {recommended_defcon}")
                logger.info(f"   Reason: {news_signal['crisis_description']}")
                logger.info(f"   News Score: {news_signal['news_score']:.1f}/100")
                self.defcon_level = recommended_defcon
                return self.defcon_level, composite_score

        self.defcon_level = base_defcon
        return self.defcon_level, composite_score

    def _check_claude_analysis(self, news_signal_id):
        """Check if Claude has provided analysis for this news signal"""
        import logging
        logger = logging.getLogger(__name__)
        
        if not news_signal_id:
            logger.debug("No news_signal_id provided to _check_claude_analysis")
            return None

        try:
            logger.info(f"  📊 Querying claude_analysis table for news_signal_id={news_signal_id}")
            self.cursor.execute("""
                SELECT enhanced_confidence, confidence_adjustment,
                       recommended_action, reasoning, opportunity_score
                FROM claude_analysis
                WHERE news_signal_id = ?
                ORDER BY created_at DESC
                LIMIT 1
            """, (news_signal_id,))

            row = self.cursor.fetchone()
            if row:
                logger.info(f"  ✅ Found Claude analysis: confidence={row[0]}, adjustment={row[1]:+.1f}")
                return {
                    'enhanced_confidence': row[0],
                    'confidence_adjustment': row[1],
                    'recommended_action': row[2],
                    'reasoning': row[3],
                    'opportunity_score': row[4]
                }
            else:
                logger.info(f"  ℹ️  No Claude analysis found for news_signal_id={news_signal_id}")
            return None

        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Error checking Claude analysis: {e}")
            return None


    def record_monitoring_point(self, yield_data, vix_data, market_data, defcon_level=None, news_signal=None, signal_score=None):
        """Record current monitoring state to database
        
        Args:
            defcon_level: Pre-calculated DEFCON level (optional, will recalculate if None)
            news_signal: News signal dict (optional)
            signal_score: Pre-calculated composite score (optional)
        """
        try:
            now = datetime.now()
            date_str = now.strftime('%Y-%m-%d')
            time_str = now.strftime('%H:%M:%S')

            # Use provided values or calculate them
            if defcon_level is None or signal_score is None:
                signal_scores = self.calculate_signal_scores(yield_data, vix_data, market_data)
                defcon_level, composite_score = self.calculate_defcon_level(
                    signal_scores,
                    market_data,
                    news_signal
                )
            else:
                composite_score = signal_score

            bond_yield = yield_data['yield'] if yield_data else None
            vix_close = vix_data['vix'] if vix_data else None
            news_score = news_signal.get('news_score', 0) if news_signal else 0

            self.cursor.execute('''
            INSERT OR REPLACE INTO signal_monitoring
            (monitoring_date, monitoring_time, bond_10yr_yield, vix_close,
             defcon_level, signal_score, news_score)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (date_str, time_str, bond_yield, vix_close, defcon_level, composite_score, news_score))

            self.conn.commit()

            return {
                'timestamp': now.isoformat(),
                'bond_yield': bond_yield,
                'vix': vix_close,
                'market_change': market_data['change_pct'] if market_data else None,
                'defcon_level': defcon_level,
                'signal_score': composite_score
            }

        except Exception as e:
            print(f"  Error recording monitoring point: {e}")
            return None

    def run_monitoring_cycle(self, verbose=True):
        """Execute one complete monitoring cycle"""
        if verbose:
            print(f"\n📊 Monitoring Cycle - {datetime.now().isoformat()}")

        # Try to fetch real-time data
        yield_data = self.fetch_bond_yield()
        vix_data = self.fetch_vix()
        market_data = self.fetch_market_prices()

        # Use fallback simulated data if real data unavailable
        data_source = "REAL"
        if not yield_data or not vix_data or not market_data:
            data_source = "SIMULATED"
            sim_data = self.get_simulated_data()
            if not yield_data:
                yield_data = sim_data['yield_data']
            if not vix_data:
                vix_data = sim_data['vix_data']
            if not market_data:
                market_data = sim_data['market_data']

        if verbose:
            print(f"  Data Source: {data_source}")
            if yield_data:
                print(f"  10Y Yield: {yield_data['yield']:.2f}%")
            if vix_data:
                print(f"  VIX: {vix_data['vix']:.1f}")
            if market_data:
                print(f"  S&P 500: {market_data['change_pct']:+.2f}%")

        # Record and analyze
        result = self.record_monitoring_point(yield_data, vix_data, market_data)

        if result:
            if verbose:
                print(f"  DEFCON Level: {result['defcon_level']}/5")
                print(f"  Signal Score: {result['signal_score']:.1f}/100")
                if result['defcon_level'] <= 3:
                    print(f"  ⚠️  ELEVATED ALERT!")

        return result

    def run_continuous(self, interval_minutes=15):
        """Run monitoring continuously at specified interval"""
        print(f"🚀 Starting continuous monitoring (interval: {interval_minutes}m)")

        try:
            self.connect()
            cycle = 0
            while True:
                cycle += 1
                print(f"\n{'='*50}")
                print(f"MONITORING CYCLE {cycle}")
                print(f"{'='*50}")

                self.run_monitoring_cycle(verbose=True)

                print(f"\nNext cycle in {interval_minutes} minutes...")
                time.sleep(interval_minutes * 60)

        except KeyboardInterrupt:
            print("\n\n✓ Monitoring stopped")
        finally:
            self.disconnect()

    def get_status(self):
        """Get current monitoring status"""
        self.connect()
        try:
            self.cursor.execute('''
            SELECT monitoring_date, monitoring_time, bond_10yr_yield, vix_close,
                   defcon_level, signal_score
            FROM signal_monitoring
            ORDER BY monitoring_date DESC, monitoring_time DESC
            LIMIT 1
            ''')
            result = self.cursor.fetchone()
            if result:
                return {
                    'date': result[0],
                    'time': result[1],
                    'bond_yield': result[2],
                    'vix': result[3],
                    'defcon_level': result[4],
                    'signal_score': result[5]
                }
        finally:
            self.disconnect()

        return None

if __name__ == '__main__':
    import sys

    monitor = SignalMonitor(DB_PATH)

    if len(sys.argv) > 1 and sys.argv[1] == 'test':
        # Test mode - single cycle
        print("🧪 Test Mode - Single Monitoring Cycle\n")
        monitor.connect()
        result = monitor.run_monitoring_cycle(verbose=True)
        monitor.disconnect()
        if result:
            print(f"\n✓ Test successful: {json.dumps(result, indent=2)}")
    elif len(sys.argv) > 1 and sys.argv[1] == 'status':
        # Status mode
        status = monitor.get_status()
        if status:
            print(f"\n📊 Current Status:")
            print(json.dumps(status, indent=2))
        else:
            print("No monitoring data yet")
    else:
        # Continuous mode (default)
        interval = int(sys.argv[1]) if len(sys.argv) > 1 else 15
        monitor.run_continuous(interval_minutes=interval)
