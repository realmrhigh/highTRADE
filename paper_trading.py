#!/usr/bin/env python3
"""
HighTrade Paper Trading Engine
Implements semi-automatic paper trading with intelligent asset selection based on crisis types
"""

import sqlite3
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
import math

# Import enhanced exit strategies
try:
    from exit_strategies import ExitStrategyManager
    EXIT_STRATEGIES_AVAILABLE = True
except ImportError:
    EXIT_STRATEGIES_AVAILABLE = False

SCRIPT_DIR = Path(__file__).parent.resolve()
DB_PATH = SCRIPT_DIR / 'trading_data' / 'trading_history.db'

logger = logging.getLogger(__name__)


class CrisisAssetIntelligence:
    """Analyzes crisis types and recommends appropriate assets to trade"""

    # Crisis-to-asset mapping based on crisis type and description
    CRISIS_PATTERNS = {
        'tech_crash': {
            'keywords': ['tech', 'valuation', 'margin', 'leverage', 'overvalued', 'correction'],
            'primary': 'VTI',
            'secondary': 'IVV',
            'tertiary': 'GOOGL',
            'rationale': 'Rotate to broad diversification during tech correction'
        },
        'geopolitical_trade': {
            'keywords': ['tariff', 'trade war', 'china', 'supply chain', 'sanctions'],
            'primary': 'QQQ',
            'secondary': 'MSFT',
            'tertiary': 'NVDA',
            'rationale': 'Tech companies resilient to tariffs; focus on IP-based business models'
        },
        'liquidity_credit': {
            'keywords': ['liquidity', 'credit', 'spread', 'financial stress', 'banking', 'crisis'],
            'primary': 'MSFT',
            'secondary': 'GOOGL',
            'tertiary': 'QQQ',
            'rationale': 'Large-cap quality less affected by credit stress'
        },
        'inflation_rate': {
            'keywords': ['inflation', 'yield', 'rate', 'fed', 'tightening', 'bonds'],
            'primary': 'QQQ',
            'secondary': 'NVDA',
            'tertiary': 'MSFT',
            'rationale': 'Growth/tech benefit from Fed policy pivot expectations'
        },
        'pandemic_health': {
            'keywords': ['pandemic', 'covid', 'disease', 'health', 'lockdown', 'epidemic'],
            'primary': 'MSFT',
            'secondary': 'GOOGL',
            'tertiary': 'NVDA',
            'rationale': 'Work-from-home and cloud infrastructure winners'
        },
        'market_correction': {
            'keywords': ['correction', 'selloff', 'drawdown', 'decline', 'drop', 'crash'],
            'primary': 'GOOGL',
            'secondary': 'NVDA',
            'tertiary': 'MSFT',
            'rationale': 'Flight to mega-cap quality and defensive positioning'
        }
    }

    def analyze_crisis_type(self, crisis_description: str, signal_score: float) -> str:
        """
        Determine crisis category from description and signal strength

        Returns: crisis_type key from CRISIS_PATTERNS
        """
        description_lower = crisis_description.lower()

        # Score each crisis pattern based on keyword matches
        pattern_scores = {}
        for pattern_type, pattern_data in self.CRISIS_PATTERNS.items():
            keyword_matches = sum(1 for kw in pattern_data['keywords']
                                if kw in description_lower)
            pattern_scores[pattern_type] = keyword_matches

        # Return pattern with highest score, default to market_correction
        if max(pattern_scores.values()) > 0:
            return max(pattern_scores, key=pattern_scores.get)
        return 'market_correction'

    def recommend_assets_for_crisis(self, crisis_type: str, signal_score: float,
                                    defcon_level: int) -> Dict[str, Any]:
        """
        Get asset recommendations for a specific crisis type

        Returns: {
            'primary_asset': str,
            'secondary_asset': str,
            'tertiary_asset': str,
            'rationale': str,
            'confidence_score': int (0-100),
            'crisis_type': str
        }
        """
        if crisis_type not in self.CRISIS_PATTERNS:
            crisis_type = 'market_correction'

        pattern = self.CRISIS_PATTERNS[crisis_type]

        # Calculate confidence based on signal strength
        base_confidence = min(100, int(signal_score))
        defcon_boost = max(0, (5 - defcon_level) * 15)  # Higher boost for lower DEFCON
        confidence = min(100, base_confidence + defcon_boost)

        return {
            'primary_asset': pattern['primary'],
            'secondary_asset': pattern['secondary'],
            'tertiary_asset': pattern['tertiary'],
            'rationale': pattern['rationale'],
            'confidence_score': confidence,
            'crisis_type': crisis_type
        }


class PaperTradingEngine:
    """
    Main paper trading system that monitors DEFCON signals and executes trades
    """

    # Configuration constants
    BASE_POSITION_SIZE = 10000  # $10,000 base position
    PROFIT_TARGET = 0.05  # +5% profit target
    STOP_LOSS = -0.03  # -3% stop loss
    MIN_POSITION_SIZE = 3000  # Minimum when VIX is very high
    MAX_POSITION_SIZE = 20000  # Maximum when VIX is very low
    MAX_CONCURRENT_SIGNALS = 3
    MAX_PORTFOLIO_EXPOSURE = 0.60  # 60% of total capital

    def __init__(self, db_path=DB_PATH, total_capital=100000):
        self.db_path = db_path
        self.total_capital = total_capital
        self.intelligence = CrisisAssetIntelligence()
        self.last_vix = 20.0
        self.pending_trade_alerts = []
        self.pending_trade_exits = []

        # Initialize enhanced exit strategy manager
        if EXIT_STRATEGIES_AVAILABLE:
            self.exit_manager = ExitStrategyManager(
                profit_target=self.PROFIT_TARGET,
                stop_loss=self.STOP_LOSS,
                trailing_stop_pct=0.02,  # 2% trailing stop
                max_hold_hours=72,  # 3 days max
                min_hold_hours=1  # At least 1 hour before allowing exits
            )
            logger.info("Enhanced exit strategies enabled")
        else:
            self.exit_manager = None
            logger.warning("Using basic exit strategies (profit target + stop loss only)")

    def connect(self):
        """Connect to database"""
        self.conn = sqlite3.connect(str(self.db_path))
        self.cursor = self.conn.cursor()
        self.cursor.row_factory = sqlite3.Row

    def disconnect(self):
        """Disconnect from database"""
        self.conn.close()

    def calculate_position_size_vix_adjusted(self, vix_level: float) -> float:
        """
        Calculate position size based on VIX volatility

        Formula: Base Position × (20 / Current VIX)
        Clamped between MIN and MAX position sizes
        """
        if vix_level <= 0:
            vix_level = 20.0  # Default fallback

        vix_adjusted = self.BASE_POSITION_SIZE * (20.0 / vix_level)
        return max(self.MIN_POSITION_SIZE, min(self.MAX_POSITION_SIZE, vix_adjusted))

    def generate_trade_alert(self, defcon_level: int, signal_score: float,
                            crisis_description: str, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Generate a trade alert with intelligent asset recommendations

        Returns: {
            'timestamp': str (ISO format),
            'defcon_level': int,
            'signal_score': float,
            'crisis_description': str,
            'assets': {
                'primary_asset': str,
                'secondary_asset': str,
                'tertiary_asset': str,
                'primary_allocation': float (0.5),
                'secondary_allocation': float (0.3),
                'tertiary_allocation': float (0.2)
            },
            'position_size': float,
            'vix': float,
            'rationale': str,
            'confidence_score': int,
            'risk_reward_analysis': str,
            'time_window_minutes': int
        }
        """
        self.last_vix = market_data.get('vix', 20.0) if market_data else 20.0

        # Analyze crisis and get asset recommendations
        crisis_type = self.intelligence.analyze_crisis_type(crisis_description, signal_score)
        recommendations = self.intelligence.recommend_assets_for_crisis(
            crisis_type, signal_score, defcon_level
        )

        # Calculate position size
        position_size = self.calculate_position_size_vix_adjusted(self.last_vix)

        # Asset allocation: 50% primary, 30% secondary, 20% tertiary
        alert = {
            'timestamp': datetime.now().isoformat(),
            'defcon_level': defcon_level,
            'signal_score': signal_score,
            'crisis_description': crisis_description,
            'crisis_type': recommendations['crisis_type'],
            'assets': {
                'primary_asset': recommendations['primary_asset'],
                'secondary_asset': recommendations['secondary_asset'],
                'tertiary_asset': recommendations['tertiary_asset'],
                'primary_allocation_pct': 0.50,
                'secondary_allocation_pct': 0.30,
                'tertiary_allocation_pct': 0.20,
                'primary_size': position_size * 0.50,
                'secondary_size': position_size * 0.30,
                'tertiary_size': position_size * 0.20
            },
            'total_position_size': position_size,
            'vix': self.last_vix,
            'rationale': recommendations['rationale'],
            'confidence_score': recommendations['confidence_score'],
            'risk_reward_analysis': self._calculate_risk_reward(defcon_level, signal_score),
            'time_window_minutes': 15
        }

        return alert

    def _calculate_risk_reward(self, defcon_level: int, signal_score: float) -> str:
        """Calculate risk/reward analysis for the trade"""
        profit_target_pct = self.PROFIT_TARGET * 100
        stop_loss_pct = abs(self.STOP_LOSS * 100)
        reward_to_risk = profit_target_pct / stop_loss_pct

        confidence_level = "LOW"
        if signal_score >= 70:
            confidence_level = "HIGH"
        elif signal_score >= 50:
            confidence_level = "MEDIUM"

        return (f"Risk: {stop_loss_pct:.1f}% | Target: +{profit_target_pct:.1f}% | "
                f"R:R Ratio: 1:{reward_to_risk:.2f} | Confidence: {confidence_level}")

    def execute_trade_package(self, alert: Dict[str, Any], user_approval: bool = True) -> List[int]:
        """
        Execute a 3-asset trade package when user approves

        Creates separate trade records for each asset, linked by crisis_id and timestamp
        Returns: list of trade_ids created [trade_id_primary, trade_id_secondary, trade_id_tertiary]
        """
        if not user_approval:
            logger.info("Trade package not executed (no user approval)")
            return []

        try:
            self.connect()

            entry_time = datetime.now()
            entry_date = entry_time.strftime('%Y-%m-%d')
            entry_time_str = entry_time.strftime('%H:%M:%S')

            # Get or create crisis event for this signal
            # Link trades by using the same timestamp and DEFCON level as identifier
            crisis_id = self._get_or_create_signal_crisis(alert, entry_date, entry_time_str)

            trade_ids = []
            assets_info = alert['assets']

            # Asset data: (asset_symbol, size_dollars, allocation_pct)
            assets = [
                (assets_info['primary_asset'], assets_info['primary_size'], assets_info['primary_allocation_pct']),
                (assets_info['secondary_asset'], assets_info['secondary_size'], assets_info['secondary_allocation_pct']),
                (assets_info['tertiary_asset'], assets_info['tertiary_size'], assets_info['tertiary_allocation_pct'])
            ]

            # Execute each asset separately
            for asset_symbol, position_size, allocation_pct in assets:
                entry_price = self._get_current_price(asset_symbol)

                if not entry_price or entry_price <= 0:
                    logger.warning(f"Could not get price for {asset_symbol}, skipping")
                    continue

                # Calculate shares
                shares = int(position_size / entry_price)
                if shares <= 0:
                    logger.warning(f"Position too small for {asset_symbol}")
                    continue

                # Insert trade record
                self.cursor.execute('''
                INSERT INTO trade_records
                (crisis_id, asset_symbol, entry_date, entry_time, entry_price,
                 entry_signal_score, defcon_at_entry, shares, position_size_dollars,
                 exit_reason, status, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    crisis_id,
                    asset_symbol,
                    entry_date,
                    entry_time_str,
                    entry_price,
                    alert['signal_score'],
                    alert['defcon_level'],
                    shares,
                    position_size,
                    None,
                    'open',
                    f"{allocation_pct*100:.0f}% of {assets_info['primary_asset']}/{assets_info['secondary_asset']}/{assets_info['tertiary_asset']} package"
                ))
                self.conn.commit()

                # Get the trade_id
                trade_id = self.cursor.lastrowid
                trade_ids.append(trade_id)
                logger.info(f"✅ Trade executed: {asset_symbol} - {shares} shares @ ${entry_price:.2f} (Trade ID: {trade_id})")

            self.conn.commit()
            return trade_ids

        except Exception as e:
            logger.error(f"Error executing trade package: {e}", exc_info=True)
            return []
        finally:
            self.disconnect()

    def monitor_all_positions(self) -> List[Dict[str, Any]]:
        """
        Monitor all open positions for exit conditions

        Checks:
        - Individual asset: +5% profit → exit that asset
        - Individual asset: -3% loss → exit that asset (stop loss)
        - Portfolio level: DEFCON reverted to 3+ → exit all open positions from that signal

        Returns: list of exit recommendations
        """
        try:
            self.connect()

            # Get all open trades
            self.cursor.execute('''
            SELECT trade_id, asset_symbol, entry_price, entry_date, entry_time,
                   defcon_at_entry, shares, position_size_dollars, crisis_id
            FROM trade_records
            WHERE status = 'open'
            ORDER BY entry_date DESC, entry_time DESC
            ''')

            open_trades = [dict(row) for row in self.cursor.fetchall()]

            if not open_trades:
                return []

            exit_recommendations = []

            # Get current DEFCON level for reversion check
            self.cursor.execute('SELECT defcon_level FROM defcon_history ORDER BY created_at DESC LIMIT 1')
            result = self.cursor.fetchone()
            current_defcon = result[0] if result else 5

            # Check each trade for exit conditions
            for trade in open_trades:
                current_price = self._get_current_price(trade['asset_symbol'])
                if not current_price or current_price <= 0:
                    continue

                # Use enhanced exit strategies if available
                if self.exit_manager:
                    exit_signal = self.exit_manager.evaluate_position(
                        trade, current_price, current_defcon
                    )

                    if exit_signal:
                        exit_recommendations.append({
                            'trade_id': exit_signal.trade_id,
                            'asset_symbol': exit_signal.asset_symbol,
                            'reason': exit_signal.reason,
                            'entry_price': exit_signal.entry_price,
                            'exit_price': exit_signal.exit_price,
                            'profit_loss_pct': exit_signal.profit_loss_pct,
                            'message': exit_signal.message,
                            'priority': exit_signal.priority
                        })
                else:
                    # Fallback to basic exit logic
                    entry_price = trade['entry_price']
                    profit_loss_pct = (current_price - entry_price) / entry_price

                    # Check profit target
                    if profit_loss_pct >= self.PROFIT_TARGET:
                        exit_recommendations.append({
                            'trade_id': trade['trade_id'],
                            'asset_symbol': trade['asset_symbol'],
                            'reason': 'profit_target',
                            'entry_price': entry_price,
                            'exit_price': current_price,
                            'profit_loss_pct': profit_loss_pct,
                            'message': f"✅ PROFIT TARGET HIT: {trade['asset_symbol']} +{profit_loss_pct*100:.2f}%",
                            'priority': 4
                        })

                    # Check stop loss
                    elif profit_loss_pct <= self.STOP_LOSS:
                        exit_recommendations.append({
                            'trade_id': trade['trade_id'],
                            'asset_symbol': trade['asset_symbol'],
                            'reason': 'stop_loss',
                            'entry_price': entry_price,
                            'exit_price': current_price,
                            'profit_loss_pct': profit_loss_pct,
                            'message': f"⚠️  STOP LOSS HIT: {trade['asset_symbol']} {profit_loss_pct*100:.2f}%",
                            'priority': 5
                        })

            # Sort by priority (highest first)
            exit_recommendations.sort(key=lambda x: x.get('priority', 0), reverse=True)

            return exit_recommendations

        except Exception as e:
            logger.error(f"Error monitoring positions: {e}", exc_info=True)
            return []
        finally:
            self.disconnect()

    def exit_position(self, trade_id: int, exit_reason: str, exit_price: Optional[float] = None) -> bool:
        """
        Exit a single position and record the result

        exit_reason: 'profit_target', 'stop_loss', 'manual', 'defcon_revert'
        """
        try:
            self.connect()

            # Get trade info
            self.cursor.execute('''
            SELECT trade_id, asset_symbol, entry_price, entry_date, entry_time,
                   shares, position_size_dollars, defcon_at_entry
            FROM trade_records
            WHERE trade_id = ? AND status = 'open'
            ''', (trade_id,))

            trade = self.cursor.fetchone()
            if not trade:
                logger.warning(f"Trade {trade_id} not found or already closed")
                return False

            trade = dict(trade)

            # Get current price if not provided
            if not exit_price:
                exit_price = self._get_current_price(trade['asset_symbol'])

            if not exit_price or exit_price <= 0:
                logger.error(f"Could not determine exit price for {trade['asset_symbol']}")
                return False

            # Calculate P&L
            profit_loss_dollars = (exit_price - trade['entry_price']) * trade['shares']
            profit_loss_percent = ((exit_price - trade['entry_price']) / trade['entry_price']) * 100

            # Calculate holding time
            entry_dt = datetime.strptime(f"{trade['entry_date']} {trade['entry_time']}", '%Y-%m-%d %H:%M:%S')
            exit_dt = datetime.now()
            holding_hours = (exit_dt - entry_dt).total_seconds() / 3600

            exit_date = exit_dt.strftime('%Y-%m-%d')
            exit_time = exit_dt.strftime('%H:%M:%S')

            # Update trade record
            self.cursor.execute('''
            UPDATE trade_records
            SET exit_date = ?, exit_time = ?, exit_price = ?, exit_reason = ?,
                profit_loss_dollars = ?, profit_loss_percent = ?, holding_hours = ?,
                status = 'closed'
            WHERE trade_id = ?
            ''', (
                exit_date, exit_time, exit_price, exit_reason,
                profit_loss_dollars, profit_loss_percent, holding_hours,
                trade_id
            ))
            self.conn.commit()

            logger.info(f"✅ Position closed: {trade['asset_symbol']} "
                       f"{profit_loss_percent:+.2f}% (${profit_loss_dollars:+,.0f})")

            # Reset trailing stop for this trade
            if self.exit_manager:
                self.exit_manager.reset_trailing_stop(trade_id)

            return True

        except Exception as e:
            logger.error(f"Error exiting position {trade_id}: {e}", exc_info=True)
            return False
        finally:
            self.disconnect()

    def get_portfolio_performance(self) -> Dict[str, Any]:
        """
        Get aggregate portfolio performance metrics

        Returns: {
            'total_trades': int,
            'open_trades': int,
            'closed_trades': int,
            'total_profit_loss_dollars': float,
            'total_profit_loss_percent': float,
            'win_rate': float (0-100),
            'profit_factor': float,
            'sharpe_ratio': float,
            'max_drawdown': float,
            'by_asset': {asset: metrics},
            'by_crisis_type': {crisis_type: metrics}
        }
        """
        try:
            self.connect()

            # Get all trades
            self.cursor.execute('''
            SELECT trade_id, asset_symbol, entry_price, exit_price, position_size_dollars,
                   profit_loss_dollars, profit_loss_percent, exit_reason, exit_date,
                   crisis_id, defcon_at_entry, status
            FROM trade_records
            ORDER BY entry_date DESC
            ''')

            all_trades = [dict(row) for row in self.cursor.fetchall()]

            closed_trades = [t for t in all_trades if t['status'] == 'closed']
            open_trades = [t for t in all_trades if t['status'] == 'open']

            # Calculate basic metrics
            total_pnl = sum(t.get('profit_loss_dollars', 0) or 0 for t in closed_trades)
            winning_trades = [t for t in closed_trades if t.get('profit_loss_dollars', 0) and t['profit_loss_dollars'] > 0]
            losing_trades = [t for t in closed_trades if t.get('profit_loss_dollars', 0) and t['profit_loss_dollars'] <= 0]

            win_rate = (len(winning_trades) / len(closed_trades) * 100) if closed_trades else 0

            # Profit factor (sum of wins / abs sum of losses)
            sum_wins = sum(t['profit_loss_dollars'] for t in winning_trades)
            sum_losses = abs(sum(t['profit_loss_dollars'] for t in losing_trades))
            profit_factor = sum_wins / sum_losses if sum_losses > 0 else 0

            # Per-asset metrics
            by_asset = {}
            for asset in set(t['asset_symbol'] for t in all_trades):
                asset_trades = [t for t in closed_trades if t['asset_symbol'] == asset]
                if asset_trades:
                    asset_pnl = sum(t.get('profit_loss_dollars', 0) or 0 for t in asset_trades)
                    asset_wins = len([t for t in asset_trades if t.get('profit_loss_dollars', 0) and t['profit_loss_dollars'] > 0])
                    by_asset[asset] = {
                        'trades': len(asset_trades),
                        'total_pnl': asset_pnl,
                        'wins': asset_wins,
                        'win_rate': (asset_wins / len(asset_trades) * 100) if asset_trades else 0
                    }

            return {
                'total_trades': len(all_trades),
                'open_trades': len(open_trades),
                'closed_trades': len(closed_trades),
                'total_profit_loss_dollars': total_pnl,
                'total_profit_loss_percent': (total_pnl / self.total_capital * 100) if self.total_capital > 0 else 0,
                'win_rate': win_rate,
                'winning_trades': len(winning_trades),
                'losing_trades': len(losing_trades),
                'profit_factor': profit_factor,
                'by_asset': by_asset,
                'timestamp': datetime.now().isoformat()
            }

        except Exception as e:
            logger.error(f"Error calculating portfolio performance: {e}", exc_info=True)
            return {
                'error': str(e),
                'total_trades': 0,
                'total_profit_loss_dollars': 0
            }
        finally:
            self.disconnect()

    def get_open_positions(self) -> List[Dict[str, Any]]:
        """Get all currently open positions"""
        try:
            self.connect()

            self.cursor.execute('''
            SELECT trade_id, asset_symbol, entry_date, entry_price, shares,
                   position_size_dollars, defcon_at_entry
            FROM trade_records
            WHERE status = 'open'
            ORDER BY entry_date DESC
            ''')

            return [dict(row) for row in self.cursor.fetchall()]

        except Exception as e:
            logger.error(f"Error fetching open positions: {e}", exc_info=True)
            return []
        finally:
            self.disconnect()

    def _get_or_create_signal_crisis(self, alert: Dict[str, Any],
                                   entry_date: str, entry_time: str) -> int:
        """
        Create a temporary crisis record for this signal event
        This links all trades in a package to the same signal event
        """
        crisis_name = f"Signal_{alert['defcon_level']}__{entry_date}_{entry_time.replace(':', '')}"

        try:
            # Check if crisis already exists for this signal
            self.cursor.execute(
                "SELECT crisis_id FROM crisis_events WHERE name LIKE ?",
                (f"Signal_{alert['defcon_level']}__{entry_date}%",)
            )
            result = self.cursor.fetchone()

            if result:
                return result[0]

            # Create new signal-based crisis record
            self.cursor.execute('''
            INSERT INTO crisis_events
            (name, description, trigger, start_date, severity, category, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                crisis_name,
                alert['crisis_description'],
                f"DEFCON {alert['defcon_level']} Signal - Score: {alert['signal_score']:.1f}",
                entry_date,
                'moderate',
                'signal',
                f"Paper trading signal package. Crisis type: {alert['crisis_type']}"
            ))
            self.conn.commit()

            return self.cursor.lastrowid

        except Exception as e:
            logger.warning(f"Error creating signal crisis: {e}")
            # Fallback: return a dummy crisis_id (can be NULL in trade_records)
            return None

    def _get_current_price(self, asset_symbol: str) -> Optional[float]:
        """
        Get current price for an asset.
        Tries yfinance first (most reliable), then Alpha Vantage as fallback.
        Returns None if both fail — callers already handle None gracefully.
        Never returns simulated/random prices to avoid phantom P&L.
        """
        # Primary: yfinance (free, no key, reliable)
        try:
            import yfinance as yf
            ticker = yf.Ticker(asset_symbol)
            hist = ticker.history(period='1d')
            if not hist.empty:
                price = float(hist['Close'].iloc[-1])
                if price > 0:
                    logger.debug(f"Fetched yfinance price for {asset_symbol}: ${price:.2f}")
                    return price
        except Exception as e:
            logger.debug(f"yfinance price fetch failed for {asset_symbol}: {e}")

        # Fallback: Alpha Vantage
        try:
            import requests
            api_key = "98ac4e761ff2e37793f310bcfb4f54c9"
            url = f"https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={asset_symbol}&apikey={api_key}"

            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                data = response.json()
                if 'Global Quote' in data and '05. price' in data['Global Quote']:
                    price = float(data['Global Quote']['05. price'])
                    if price > 0:
                        logger.debug(f"Fetched Alpha Vantage price for {asset_symbol}: ${price:.2f}")
                        return price

        except Exception as e:
            logger.debug(f"Alpha Vantage price fetch failed for {asset_symbol}: {e}")

        logger.warning(f"All price sources failed for {asset_symbol} — returning None (no simulated fallback)")
        return None

    def manual_buy(self, ticker: str, shares: int,
                   price_override: float = None, notes: str = '') -> dict:
        """
        Execute a manual paper buy for any ticker/share count.
        Used by the /buy slash command.

        Args:
            ticker:         Stock symbol (e.g. 'MSOS', 'AAPL')
            shares:         Number of shares to buy
            price_override: Optional fixed entry price (skips live fetch)
            notes:          Optional note stored with the trade

        Returns:
            dict with ok, trade_id, message, entry_price, position_size
        """
        ticker = ticker.upper().strip()

        if shares <= 0:
            return {'ok': False, 'message': 'Shares must be a positive integer.'}

        # Fetch live price (or use override)
        if price_override and price_override > 0:
            entry_price = price_override
            price_source = 'manual override'
        else:
            entry_price = self._get_current_price(ticker)
            price_source = 'live'

        if not entry_price or entry_price <= 0:
            return {'ok': False, 'message': f'Could not fetch price for {ticker}.'}

        position_size = round(entry_price * shares, 2)
        entry_time = datetime.now()
        entry_date = entry_time.strftime('%Y-%m-%d')
        entry_time_str = entry_time.strftime('%H:%M:%S')

        try:
            self.connect()

            # Use crisis_id = 0 for manual trades (no signal event)
            self.cursor.execute('''
                INSERT INTO trade_records
                (crisis_id, asset_symbol, entry_date, entry_time, entry_price,
                 entry_signal_score, defcon_at_entry, shares, position_size_dollars,
                 exit_reason, status, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                0,                              # crisis_id = 0 → manual
                ticker,
                entry_date,
                entry_time_str,
                entry_price,
                0,                              # signal_score: N/A for manual
                5,                              # defcon: N/A, default 5
                shares,
                position_size,
                None,                           # exit_reason: null until closed
                'open',
                notes or f'Manual buy via /buy command ({price_source} price)'
            ))
            self.conn.commit()
            trade_id = self.cursor.lastrowid

            logger.info(
                f"✅ Manual buy executed: {shares} × {ticker} @ ${entry_price:.2f} "
                f"= ${position_size:,.2f} (trade_id={trade_id})"
            )
            return {
                'ok': True,
                'trade_id': trade_id,
                'ticker': ticker,
                'shares': shares,
                'entry_price': entry_price,
                'position_size': position_size,
                'message': (
                    f"Bought {shares} shares of {ticker} @ ${entry_price:.2f} "
                    f"= ${position_size:,.2f} paper position (trade #{trade_id})"
                )
            }

        except Exception as e:
            logger.error(f"Manual buy failed: {e}", exc_info=True)
            return {'ok': False, 'message': f'Trade execution failed: {e}'}
        finally:
            self.disconnect()

    def manual_sell(self, ticker: str, trade_id: int = None,
                    price_override: float = None) -> dict:
        """
        Exit a manual or system paper position.
        Used by the /sell slash command.

        Args:
            ticker:         Stock symbol to exit (closes most recent open position if trade_id omitted)
            trade_id:       Specific trade_id to close (optional)
            price_override: Optional fixed exit price

        Returns:
            dict with ok, message, pnl_dollars, pnl_pct
        """
        ticker = ticker.upper().strip()

        try:
            self.connect()

            # Find the trade to close
            if trade_id:
                self.cursor.execute(
                    "SELECT * FROM trade_records WHERE trade_id=? AND status='open'", (trade_id,)
                )
            else:
                self.cursor.execute(
                    "SELECT * FROM trade_records WHERE asset_symbol=? AND status='open' "
                    "ORDER BY entry_date DESC, entry_time DESC LIMIT 1",
                    (ticker,)
                )

            row = self.cursor.fetchone()
            if not row:
                return {
                    'ok': False,
                    'message': f'No open position found for {ticker}' +
                               (f' (trade #{trade_id})' if trade_id else '')
                }

            trade = dict(row)
            actual_trade_id = trade['trade_id']
            entry_price = trade['entry_price']
            shares = trade['shares']

            # Get exit price
            if price_override and price_override > 0:
                exit_price = price_override
            else:
                exit_price = self._get_current_price(ticker)

            if not exit_price or exit_price <= 0:
                return {'ok': False, 'message': f'Could not fetch exit price for {ticker}.'}

            pnl_dollars = round((exit_price - entry_price) * shares, 2)
            pnl_pct = round(((exit_price - entry_price) / entry_price) * 100, 4)
            exit_time = datetime.now()

            self.cursor.execute('''
                UPDATE trade_records
                SET exit_date=?, exit_time=?, exit_price=?, exit_reason=?,
                    profit_loss_dollars=?, profit_loss_percent=?, status=?
                WHERE trade_id=?
            ''', (
                exit_time.strftime('%Y-%m-%d'),
                exit_time.strftime('%H:%M:%S'),
                exit_price,
                'manual',
                pnl_dollars,
                pnl_pct,
                'closed',
                actual_trade_id
            ))
            self.conn.commit()

            direction = '📈' if pnl_dollars >= 0 else '📉'
            logger.info(
                f"{direction} Manual sell: {shares} × {ticker} @ ${exit_price:.2f} | "
                f"P&L: ${pnl_dollars:+,.2f} ({pnl_pct:+.2f}%) (trade #{actual_trade_id})"
            )
            return {
                'ok': True,
                'trade_id': actual_trade_id,
                'ticker': ticker,
                'shares': shares,
                'entry_price': entry_price,
                'exit_price': exit_price,
                'pnl_dollars': pnl_dollars,
                'pnl_pct': pnl_pct,
                'message': (
                    f"Sold {shares} shares of {ticker} @ ${exit_price:.2f} | "
                    f"P&L: ${pnl_dollars:+,.2f} ({pnl_pct:+.2f}%)"
                )
            }

        except Exception as e:
            logger.error(f"Manual sell failed: {e}", exc_info=True)
            return {'ok': False, 'message': f'Sell execution failed: {e}'}
        finally:
            self.disconnect()


def main():
    """Test the paper trading engine"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )

    engine = PaperTradingEngine()

    # Test alert generation
    test_alert = engine.generate_trade_alert(
        defcon_level=2,
        signal_score=75.0,
        crisis_description="Tariff announcement causing supply chain concerns",
        market_data={'vix': 25.5}
    )

    print("\n" + "="*70)
    print("PAPER TRADING ENGINE TEST")
    print("="*70)
    print(json.dumps(test_alert, indent=2))
    print("="*70 + "\n")


if __name__ == '__main__':
    main()
