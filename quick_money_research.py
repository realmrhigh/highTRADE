#!/usr/bin/env python3
"""
Quick Money Market Research - Find Short-Term Trading Opportunities
Analyzes stocks with high volatility, momentum, and volume for quick flips
"""

import logging
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from pathlib import Path
import json
import sqlite3

logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent.resolve()
DB_PATH = SCRIPT_DIR / 'trading_data' / 'trading_history.db'
CACHE_PATH = SCRIPT_DIR / 'trading_data' / 'quick_money_cache.json'


class QuickMoneyResearch:
    """Find potential stocks for quick flips based on technical indicators"""

    # Focus stocks: High liquidity, volatile enough for quick gains
    CANDIDATE_POOLS = {
        'mega_tech': ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'META', 'NVDA', 'TSLA'],
        'high_beta': ['AMD', 'PLTR', 'COIN', 'ROKU', 'SNAP', 'UBER', 'LYFT'],
        'etfs': ['QQQ', 'SPY', 'IWM', 'XLF', 'XLE', 'ARKK', 'SQQQ', 'TQQQ'],
        'volatile_plays': ['GME', 'AMC', 'RIOT', 'MARA', 'BB', 'NOK']
    }

    # Quick flip criteria
    QUICK_FLIP_THRESHOLDS = {
        'min_volume': 1_000_000,        # Minimum daily volume for liquidity
        'target_gain': 0.02,             # 2% gain target (quick flip)
        'max_hold_time': 1,              # Max 1 day hold
        'min_volatility': 0.015,         # 1.5% daily volatility minimum
        'momentum_threshold': 0.01,      # 1% momentum in right direction
        'rsi_oversold': 30,              # RSI oversold level
        'rsi_overbought': 70,            # RSI overbought level
    }

    def __init__(self):
        self.cache = self._load_cache()
        self.opportunities = []

    def research_quick_flip_opportunities(self) -> List[Dict]:
        """
        Main research function to find quick flip opportunities

        Returns: List of trading opportunities ranked by potential
        """
        logger.info("🔍 Starting Quick Money Research...")
        logger.info("="*60)

        all_candidates = []
        
        # Gather all candidate stocks
        for pool_name, symbols in self.CANDIDATE_POOLS.items():
            logger.info(f"Scanning {pool_name}: {', '.join(symbols)}")
            for symbol in symbols:
                candidate = self._analyze_symbol(symbol, pool_name)
                if candidate:
                    all_candidates.append(candidate)

        # Filter and rank opportunities
        opportunities = self._filter_and_rank(all_candidates)
        
        # Cache results
        self._save_cache(opportunities)
        
        self.opportunities = opportunities
        
        logger.info(f"\n✅ Found {len(opportunities)} quick flip opportunities")
        return opportunities

    def _analyze_symbol(self, symbol: str, pool_name: str) -> Optional[Dict]:
        """Analyze a single symbol for quick flip potential"""
        try:
            # Get current price and recent data
            price_data = self._get_price_data(symbol)
            if not price_data:
                return None

            # Calculate technical indicators
            volatility = self._calculate_volatility(price_data)
            momentum = self._calculate_momentum(price_data)
            rsi = self._calculate_rsi(price_data)
            volume_surge = self._check_volume_surge(price_data)
            
            # Get current price
            current_price = price_data['current_price']
            
            # Determine if it's a buy opportunity
            signal = self._generate_signal(volatility, momentum, rsi, volume_surge)
            
            if not signal['is_opportunity']:
                return None

            # Calculate quick flip parameters
            entry_price = current_price
            target_price = entry_price * (1 + self.QUICK_FLIP_THRESHOLDS['target_gain'])
            stop_loss = entry_price * 0.99  # 1% stop loss for quick flips
            
            opportunity = {
                'symbol': symbol,
                'pool': pool_name,
                'current_price': current_price,
                'entry_price': entry_price,
                'target_price': target_price,
                'stop_loss': stop_loss,
                'expected_gain_pct': 2.0,
                'volatility': volatility,
                'momentum': momentum,
                'rsi': rsi,
                'volume_surge': volume_surge,
                'signal_type': signal['type'],
                'signal_strength': signal['strength'],
                'confidence': signal['confidence'],
                'rationale': signal['rationale'],
                'max_hold_days': 1,
                'timestamp': datetime.now().isoformat()
            }
            
            logger.info(f"  ✓ {symbol}: {signal['type']} - Confidence: {signal['confidence']}%")
            return opportunity

        except Exception as e:
            logger.warning(f"  ⚠️  {symbol}: Error analyzing - {e}")
            return None

    def _get_price_data(self, symbol: str) -> Optional[Dict]:
        """
        Fetch recent price data for symbol
        
        Uses Yahoo Finance API (free, no key required)
        """
        try:
            # Yahoo Finance API endpoint
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            params = {
                'interval': '1d',
                'range': '1mo'
            }
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
            }
            
            response = requests.get(url, params=params, headers=headers, timeout=10)
            
            if response.status_code != 200:
                return None
                
            data = response.json()
            
            if 'chart' not in data or 'result' not in data['chart']:
                return None
                
            result = data['chart']['result'][0]
            
            # Extract price and volume data
            quotes = result['indicators']['quote'][0]
            timestamps = result['timestamp']
            
            closes = [c for c in quotes['close'] if c is not None]
            volumes = [v for v in quotes['volume'] if v is not None]
            
            if not closes or len(closes) < 5:
                return None
            
            return {
                'symbol': symbol,
                'current_price': closes[-1],
                'closes': closes,
                'volumes': volumes,
                'timestamps': timestamps
            }
            
        except Exception as e:
            logger.debug(f"Error fetching {symbol}: {e}")
            return None

    def _calculate_volatility(self, price_data: Dict) -> float:
        """Calculate recent volatility (standard deviation of returns)"""
        closes = price_data['closes']
        
        if len(closes) < 5:
            return 0.0
        
        # Calculate daily returns
        returns = []
        for i in range(1, len(closes)):
            if closes[i-1] > 0:
                ret = (closes[i] - closes[i-1]) / closes[i-1]
                returns.append(ret)
        
        if not returns:
            return 0.0
        
        # Standard deviation of returns
        mean_return = sum(returns) / len(returns)
        variance = sum((r - mean_return) ** 2 for r in returns) / len(returns)
        volatility = variance ** 0.5
        
        return volatility

    def _calculate_momentum(self, price_data: Dict) -> float:
        """Calculate recent momentum (5-day price change rate)"""
        closes = price_data['closes']
        
        if len(closes) < 5:
            return 0.0
        
        # 5-day momentum
        five_day_ago = closes[-6] if len(closes) > 5 else closes[0]
        current = closes[-1]
        
        if five_day_ago <= 0:
            return 0.0
        
        momentum = (current - five_day_ago) / five_day_ago
        return momentum

    def _calculate_rsi(self, price_data: Dict, periods: int = 14) -> float:
        """Calculate Relative Strength Index"""
        closes = price_data['closes']
        
        if len(closes) < periods + 1:
            return 50.0  # Neutral RSI
        
        # Calculate price changes
        changes = []
        for i in range(1, len(closes)):
            changes.append(closes[i] - closes[i-1])
        
        # Separate gains and losses
        gains = [max(c, 0) for c in changes[-periods:]]
        losses = [abs(min(c, 0)) for c in changes[-periods:]]
        
        avg_gain = sum(gains) / periods if gains else 0
        avg_loss = sum(losses) / periods if losses else 0
        
        if avg_loss == 0:
            return 100.0
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return rsi

    def _check_volume_surge(self, price_data: Dict) -> float:
        """Check if recent volume is surging (compared to average)"""
        volumes = price_data['volumes']
        
        if len(volumes) < 5:
            return 1.0
        
        avg_volume = sum(volumes[:-1]) / len(volumes[:-1])
        recent_volume = volumes[-1]
        
        if avg_volume <= 0:
            return 1.0
        
        volume_ratio = recent_volume / avg_volume
        return volume_ratio

    def _generate_signal(self, volatility: float, momentum: float, 
                        rsi: float, volume_surge: float) -> Dict:
        """
        Generate trading signal based on technical indicators
        
        Returns: {
            'is_opportunity': bool,
            'type': str (MOMENTUM_BUY, OVERSOLD_BOUNCE, etc),
            'strength': str (STRONG, MODERATE, WEAK),
            'confidence': int (0-100),
            'rationale': str
        }
        """
        thresholds = self.QUICK_FLIP_THRESHOLDS
        
        # Score components
        scores = []
        reasons = []
        
        # 1. Check volatility (need movement for quick flips)
        if volatility >= thresholds['min_volatility']:
            scores.append(25)
            reasons.append(f"High volatility ({volatility*100:.1f}%)")
        else:
            scores.append(0)
        
        # 2. Check momentum
        if momentum >= thresholds['momentum_threshold']:
            scores.append(30)
            signal_type = "MOMENTUM_BUY"
            reasons.append(f"Positive momentum (+{momentum*100:.1f}%)")
        elif momentum <= -thresholds['momentum_threshold']:
            scores.append(20)
            signal_type = "REVERSAL_PLAY"
            reasons.append(f"Potential reversal ({momentum*100:.1f}%)")
        else:
            scores.append(5)
            signal_type = "RANGE_TRADE"
        
        # 3. Check RSI
        if rsi <= thresholds['rsi_oversold']:
            scores.append(30)
            signal_type = "OVERSOLD_BOUNCE"
            reasons.append(f"RSI oversold ({rsi:.1f})")
        elif rsi >= thresholds['rsi_overbought']:
            scores.append(10)
            reasons.append(f"RSI overbought ({rsi:.1f}) - risky")
        else:
            scores.append(15)
            reasons.append(f"RSI neutral ({rsi:.1f})")
        
        # 4. Check volume
        if volume_surge >= 1.5:
            scores.append(15)
            reasons.append(f"Volume surge ({volume_surge:.1f}x)")
        elif volume_surge >= 1.2:
            scores.append(10)
        else:
            scores.append(5)
        
        # Calculate confidence
        confidence = sum(scores)
        
        # Determine if it's an opportunity
        is_opportunity = confidence >= 50
        
        # Determine strength
        if confidence >= 75:
            strength = "STRONG"
        elif confidence >= 60:
            strength = "MODERATE"
        else:
            strength = "WEAK"
        
        return {
            'is_opportunity': is_opportunity,
            'type': signal_type,
            'strength': strength,
            'confidence': confidence,
            'rationale': " | ".join(reasons)
        }

    def _filter_and_rank(self, candidates: List[Dict]) -> List[Dict]:
        """Filter and rank opportunities by confidence"""
        # Filter minimum confidence
        filtered = [c for c in candidates if c['confidence'] >= 50]
        
        # Rank by confidence score
        ranked = sorted(filtered, key=lambda x: x['confidence'], reverse=True)
        
        return ranked

    def get_top_opportunities(self, n: int = 5) -> List[Dict]:
        """Get top N quick flip opportunities"""
        if not self.opportunities:
            self.research_quick_flip_opportunities()
        
        return self.opportunities[:n]

    def print_opportunities_report(self):
        """Print formatted report of opportunities"""
        if not self.opportunities:
            logger.info("No opportunities found. Run research first.")
            return

        print("\n" + "="*80)
        print("💰 QUICK MONEY OPPORTUNITIES - SHORT-TERM FLIP ANALYSIS")
        print("="*80)
        print(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Total Opportunities: {len(self.opportunities)}")
        print("="*80)

        for i, opp in enumerate(self.opportunities[:10], 1):
            print(f"\n#{i}. {opp['symbol']} - {opp['signal_type']}")
            print(f"   Pool: {opp['pool']}")
            print(f"   Confidence: {opp['confidence']}% ({opp['signal_strength']})")
            print(f"   Current Price: ${opp['current_price']:.2f}")
            print(f"   Target Price: ${opp['target_price']:.2f} (+{opp['expected_gain_pct']:.1f}%)")
            print(f"   Stop Loss: ${opp['stop_loss']:.2f} (-1.0%)")
            print(f"   Max Hold: {opp['max_hold_days']} day")
            print(f"   Indicators:")
            print(f"      - RSI: {opp['rsi']:.1f}")
            print(f"      - Volatility: {opp['volatility']*100:.2f}%")
            print(f"      - Momentum: {opp['momentum']*100:.2f}%")
            print(f"      - Volume Surge: {opp['volume_surge']:.1f}x")
            print(f"   Rationale: {opp['rationale']}")

        print("\n" + "="*80)
        print("⚠️  QUICK FLIP TRADING TIPS:")
        print("   • Set tight stop losses (1-2%)")
        print("   • Take profits quickly (2-3% targets)")
        print("   • Don't hold overnight if momentum weakens")
        print("   • Watch for volume confirmation on entry")
        print("   • Best for day trading or 1-day swings")
        print("="*80 + "\n")

    def _load_cache(self) -> Dict:
        """Load cached research results"""
        try:
            if CACHE_PATH.exists():
                with open(CACHE_PATH, 'r') as f:
                    return json.load(f)
        except Exception as e:
            logger.debug(f"Could not load cache: {e}")
        return {}

    def _save_cache(self, opportunities: List[Dict]):
        """Save research results to cache"""
        try:
            cache_data = {
                'timestamp': datetime.now().isoformat(),
                'opportunities': opportunities
            }
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(CACHE_PATH, 'w') as f:
                json.dump(cache_data, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not save cache: {e}")

    def record_trade_to_db(self, opportunity: Dict, action: str = 'planned'):
        """Record quick flip trade to database for tracking"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            
            # Create quick_flips table if doesn't exist
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS quick_flips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                entry_price REAL,
                target_price REAL,
                stop_loss REAL,
                confidence INTEGER,
                signal_type TEXT,
                action TEXT,
                status TEXT DEFAULT 'planned',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')
            
            cursor.execute('''
            INSERT INTO quick_flips (
                symbol, entry_price, target_price, stop_loss,
                confidence, signal_type, action, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                opportunity['symbol'],
                opportunity['entry_price'],
                opportunity['target_price'],
                opportunity['stop_loss'],
                opportunity['confidence'],
                opportunity['signal_type'],
                action,
                'planned'
            ))
            
            conn.commit()
            conn.close()
            
            logger.info(f"✅ Recorded {opportunity['symbol']} quick flip to database")
            
        except Exception as e:
            logger.error(f"Error recording to database: {e}")


def main():
    """Run quick money research"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    research = QuickMoneyResearch()
    research.research_quick_flip_opportunities()
    research.print_opportunities_report()


if __name__ == '__main__':
    main()
