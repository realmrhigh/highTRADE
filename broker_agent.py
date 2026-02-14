#!/usr/bin/env python3
"""
HighTrade Broker Agent - Autonomous Trading Decision System
Analyzes market conditions, makes trade decisions, and executes on your behalf
"""

import sqlite3
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from paper_trading import PaperTradingEngine, CrisisAssetIntelligence
from alerts import AlertSystem
from quick_money_research import QuickMoneyResearch

DB_PATH = Path.home() / 'trading_data' / 'trading_history.db'

logger = logging.getLogger(__name__)


class BrokerDecisionEngine:
    """Makes autonomous trading decisions"""

    def __init__(self):
        self.paper_trading = PaperTradingEngine()
        self.intelligence = CrisisAssetIntelligence()
        self.alerts = AlertSystem()
        self.quick_money = QuickMoneyResearch()
        self.decision_history = []

    def analyze_market_for_trades(self, defcon_level: int, signal_score: float,
                                  crisis_description: str, market_data: Dict) -> Optional[Dict]:
        """
        Analyze market conditions and decide whether to execute a trade

        Returns trade decision or None if no trade warranted
        """
        vix = market_data.get('vix', 20.0)

        # Decision 1: Should we trade at all?
        if not self._should_trade(defcon_level, signal_score):
            logger.info("❌ Trade criteria not met - skipping")
            return None

        # Decision 2: What assets should we trade?
        crisis_type = self.intelligence.analyze_crisis_type(crisis_description, signal_score)
        recommendations = self.intelligence.recommend_assets_for_crisis(
            crisis_type, signal_score, defcon_level
        )

        # Decision 3: How much should we trade?
        position_size = self.paper_trading.calculate_position_size_vix_adjusted(vix)

        # Decision 4: Risk check - don't over-expose
        current_exposure = self._calculate_current_exposure()
        if current_exposure + position_size > self.paper_trading.total_capital * 0.60:
            logger.warning(f"⚠️  Portfolio exposure limit reached ({current_exposure + position_size:.0f})")
            return None

        # Build trade decision
        decision = {
            'timestamp': datetime.now().isoformat(),
            'decision_type': 'BUY_PACKAGE',
            'confidence': recommendations['confidence_score'],
            'crisis_type': crisis_type,
            'assets': {
                'primary': recommendations['primary_asset'],
                'secondary': recommendations['secondary_asset'],
                'tertiary': recommendations['tertiary_asset']
            },
            'position_size': position_size,
            'vix': vix,
            'defcon_level': defcon_level,
            'signal_score': signal_score,
            'rationale': recommendations['rationale']
        }

        logger.info(f"✅ BUY DECISION: {crisis_type} - Size: ${position_size:,.0f}, Confidence: {decision['confidence']}%")
        return decision

    def analyze_positions_for_exits(self) -> List[Dict]:
        """
        Analyze all open positions and decide which ones to exit

        Returns list of exit decisions
        """
        exit_decisions = []

        # Get all open positions
        self.paper_trading.connect()
        try:
            self.paper_trading.cursor.execute('''
            SELECT trade_id, asset_symbol, entry_price, position_size_dollars,
                   defcon_at_entry, shares, entry_date
            FROM trade_records
            WHERE status = 'open'
            ''')

            open_trades = [dict(row) for row in self.paper_trading.cursor.fetchall()]
        finally:
            self.paper_trading.disconnect()

        # Analyze each position
        for trade in open_trades:
            current_price = self.paper_trading._get_current_price(trade['asset_symbol'])
            if not current_price or current_price <= 0:
                continue

            entry_price = trade['entry_price']
            profit_loss_pct = (current_price - entry_price) / entry_price
            profit_loss_dollars = profit_loss_pct * trade['position_size_dollars']

            # Decision 1: Hit profit target?
            if profit_loss_pct >= self.paper_trading.PROFIT_TARGET:
                decision = {
                    'trade_id': trade['trade_id'],
                    'asset_symbol': trade['asset_symbol'],
                    'decision_type': 'SELL_PROFIT_TARGET',
                    'entry_price': entry_price,
                    'current_price': current_price,
                    'profit_loss_pct': profit_loss_pct,
                    'profit_loss_dollars': profit_loss_dollars,
                    'reason': f"Hit profit target: +{profit_loss_pct*100:.2f}%",
                    'confidence': 100
                }
                exit_decisions.append(decision)
                logger.info(f"📈 EXIT: {trade['asset_symbol']} - Profit target hit! +{profit_loss_pct*100:.2f}%")

            # Decision 2: Hit stop loss?
            elif profit_loss_pct <= self.paper_trading.STOP_LOSS:
                decision = {
                    'trade_id': trade['trade_id'],
                    'asset_symbol': trade['asset_symbol'],
                    'decision_type': 'SELL_STOP_LOSS',
                    'entry_price': entry_price,
                    'current_price': current_price,
                    'profit_loss_pct': profit_loss_pct,
                    'profit_loss_dollars': profit_loss_dollars,
                    'reason': f"Stop loss triggered: {profit_loss_pct*100:.2f}%",
                    'confidence': 100
                }
                exit_decisions.append(decision)
                logger.warning(f"🛑 EXIT: {trade['asset_symbol']} - Stop loss! {profit_loss_pct*100:.2f}%")

            # Decision 3: Should we take early profit?
            elif self._should_take_early_profit(profit_loss_pct, trade):
                decision = {
                    'trade_id': trade['trade_id'],
                    'asset_symbol': trade['asset_symbol'],
                    'decision_type': 'SELL_EARLY_PROFIT',
                    'entry_price': entry_price,
                    'current_price': current_price,
                    'profit_loss_pct': profit_loss_pct,
                    'profit_loss_dollars': profit_loss_dollars,
                    'reason': f"Early profit opportunity: +{profit_loss_pct*100:.2f}%",
                    'confidence': 75
                }
                exit_decisions.append(decision)
                logger.info(f"💰 EARLY EXIT: {trade['asset_symbol']} - Taking early profit +{profit_loss_pct*100:.2f}%")

        return exit_decisions

    def get_buy_recommendations(self, top_n: int = 3) -> List[Dict]:
        """
        Get top buy recommendations for specific assets

        Analyzes which assets have been most profitable and recommends buying more
        """
        recommendations = []

        # Get asset performance
        self.paper_trading.connect()
        try:
            self.paper_trading.cursor.execute('''
            SELECT
                asset_symbol,
                COUNT(*) as total_trades,
                SUM(CASE WHEN profit_loss_dollars > 0 THEN 1 ELSE 0 END) as winners,
                AVG(profit_loss_percent) as avg_return
            FROM trade_records
            WHERE status = 'closed'
            GROUP BY asset_symbol
            ORDER BY avg_return DESC
            LIMIT ?
            ''', (top_n,))

            top_assets = [dict(row) for row in self.paper_trading.cursor.fetchall()]
        finally:
            self.paper_trading.disconnect()

        # Create recommendations
        for i, asset in enumerate(top_assets, 1):
            recommendation = {
                'rank': i,
                'asset': asset['asset_symbol'],
                'past_trades': asset['total_trades'],
                'win_rate': (asset['winners'] / asset['total_trades'] * 100) if asset['total_trades'] > 0 else 0,
                'avg_return': asset['avg_return'],
                'reason': f"Best performer: {asset['avg_return']:.2f}% avg return",
                'confidence': 60
            }
            recommendations.append(recommendation)
            logger.info(f"💡 RECOMMENDATION #{i}: {asset['asset_symbol']} - "
                       f"Avg return: {asset['avg_return']:.2f}%, Win rate: {recommendation['win_rate']:.0f}%")

        return recommendations

    def get_quick_money_opportunities(self, top_n: int = 5) -> List[Dict]:
        """
        Get quick flip opportunities for rapid trading
        
        Returns list of high-potential short-term trades
        """
        logger.info("🔍 Scanning for quick money opportunities...")
        
        try:
            opportunities = self.quick_money.research_quick_flip_opportunities()
            top_opps = opportunities[:top_n]
            
            if top_opps:
                logger.info(f"💰 Found {len(top_opps)} quick flip opportunities")
                for i, opp in enumerate(top_opps, 1):
                    logger.info(f"  #{i}: {opp['symbol']} - {opp['signal_type']} "
                              f"(Confidence: {opp['confidence']}%)")
            else:
                logger.info("No quick flip opportunities meet criteria")
                
            return top_opps
            
        except Exception as e:
            logger.error(f"Error during quick money research: {e}")
            return []

    def analyze_quick_flip_entry(self, opportunity: Dict) -> Optional[Dict]:
        """
        Analyze if we should enter a quick flip trade
        
        Returns trade decision or None
        """
        # Check if we have capital for quick flip
        position_size = self.paper_trading.total_capital * 0.10  # 10% for quick flips
        
        current_exposure = self._calculate_current_exposure()
        if current_exposure + position_size > self.paper_trading.total_capital * 0.70:
            logger.warning(f"⚠️  Exposure limit - skipping quick flip {opportunity['symbol']}")
            return None
        
        # Build quick flip decision
        decision = {
            'timestamp': datetime.now().isoformat(),
            'decision_type': 'QUICK_FLIP_BUY',
            'trade_type': 'quick_flip',
            'symbol': opportunity['symbol'],
            'confidence': opportunity['confidence'],
            'signal_type': opportunity['signal_type'],
            'entry_price': opportunity['entry_price'],
            'target_price': opportunity['target_price'],
            'stop_loss': opportunity['stop_loss'],
            'position_size': position_size,
            'expected_gain_pct': opportunity['expected_gain_pct'],
            'max_hold_days': opportunity['max_hold_days'],
            'rationale': opportunity['rationale'],
            'volatility': opportunity['volatility'],
            'momentum': opportunity['momentum'],
            'rsi': opportunity['rsi']
        }
        
        logger.info(f"✅ QUICK FLIP BUY: {opportunity['symbol']} - "
                   f"Target: +{opportunity['expected_gain_pct']:.1f}%, "
                   f"Confidence: {opportunity['confidence']}%")
        
        return decision

    def _should_trade(self, defcon_level: int, signal_score: float) -> bool:
        """Determine if we should execute a trade"""
        # Don't trade during DEFCON 5 (peaceful times)
        if defcon_level > 2:
            return False

        # Don't trade if signal score too low
        if signal_score < 60:
            return False

        return True

    def _should_take_early_profit(self, profit_loss_pct: float, trade: Dict) -> bool:
        """Decide if we should take early profit before target"""
        # If up 3-4%, consider taking profit
        if 0.03 <= profit_loss_pct < 0.05:
            # But only if we're confident it will give back
            # For now, be conservative
            return False

        return False

    def _calculate_current_exposure(self) -> float:
        """Calculate total current portfolio exposure"""
        self.paper_trading.connect()
        try:
            self.paper_trading.cursor.execute('''
            SELECT SUM(position_size_dollars) as total
            FROM trade_records
            WHERE status = 'open'
            ''')
            result = self.paper_trading.cursor.fetchone()
            return result[0] if result[0] else 0
        finally:
            self.paper_trading.disconnect()

    def record_decision(self, decision: Dict, executed: bool = False, result: Optional[str] = None):
        """Record a trading decision in history"""
        self.decision_history.append({
            'timestamp': datetime.now().isoformat(),
            'decision': decision,
            'executed': executed,
            'result': result
        })

        if executed:
            logger.info(f"✅ DECISION EXECUTED: {decision.get('decision_type')}")
        else:
            logger.info(f"⏭️  DECISION SKIPPED: {decision.get('decision_type')}")


class BrokerNotificationEngine:
    """Handles notifications and tips for the user"""

    def __init__(self):
        self.alerts = AlertSystem()
        self.trade_engine = PaperTradingEngine()

    def send_buy_notification(self, decision: Dict):
        """Notify user about a buy decision"""
        message = f"""
🎯 BROKER ACTION: BUY SIGNAL
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Decision: Autonomous Buy Executed
Time: {decision['timestamp']}

Crisis Type: {decision['crisis_type']}
DEFCON Level: {decision['defcon_level']}/5
Signal Score: {decision['signal_score']:.1f}/100
Confidence: {decision['confidence']}%

Assets Purchased:
  🔹 Primary (50%): {decision['assets']['primary']}
  🔹 Secondary (30%): {decision['assets']['secondary']}
  🔹 Tertiary (20%): {decision['assets']['tertiary']}

Position Size: ${decision['position_size']:,.0f}
VIX Level: {decision['vix']:.1f}

Rationale: {decision['rationale']}

Exit Strategy:
  ✓ Profit Target: +5%
  ✓ Stop Loss: -3%
  ✓ DEFCON Revert: Exit all

Your broker made this decision on your behalf.
Monitor portfolio: python3 trading_cli.py status
"""
        self.alerts.send_defcon_alert(
            defcon_level=decision['defcon_level'],
            signal_score=decision['signal_score'],
            details=message
        )
        logger.info("📨 Buy notification sent")

    def send_sell_notification(self, decision: Dict):
        """Notify user about a sell decision"""
        profit_loss_color = "📈" if decision['profit_loss_dollars'] > 0 else "📉"

        message = f"""
💼 BROKER ACTION: SELL EXECUTED
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Decision: Autonomous Sell Executed
Time: {datetime.now().isoformat()}
Reason: {decision['reason']}

Asset: {decision['asset_symbol']}
Trade ID: {decision['trade_id']}

Entry Price: ${decision['entry_price']:.2f}
Exit Price: ${decision['current_price']:.2f}

Result:
  {profit_loss_color} Profit/Loss: ${decision['profit_loss_dollars']:+,.0f}
  {profit_loss_color} Return: {decision['profit_loss_pct']:+.2f}%

Exit Type: {decision['decision_type']}

Your broker closed this position on your behalf.
Check portfolio: python3 trading_cli.py status
"""
        # Send via all enabled channels (Slack, email, etc)
        self.alerts.send_defcon_alert(
            defcon_level=1,
            signal_score=decision.get('confidence', 100),
            details=message
        )
        logger.info("📨 Sell notification sent to all channels")

    def send_tip(self, tip_type: str, content: str):
        """Send trading tips to user"""
        tips_message = f"""
💡 BROKER TIP
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Type: {tip_type}
Time: {datetime.now().isoformat()}

{content}

Questions? Check the documentation:
  • PAPER_TRADING_GUIDE.md
  • trading_cli.py status
"""
        logger.info(f"💡 Tip: {tip_type}")
        logger.info(tips_message)


class AutonomousBroker:
    """Main autonomous broker that makes and executes trades"""

    def __init__(self, auto_execute: bool = True, max_daily_trades: int = 5):
        self.decision_engine = BrokerDecisionEngine()
        self.notification_engine = BrokerNotificationEngine()
        self.auto_execute = auto_execute
        self.max_daily_trades = max_daily_trades
        self.trades_executed_today = 0
        self.last_reset = datetime.now().date()

    def process_market_conditions(self, defcon_level: int, signal_score: float,
                                 crisis_description: str, market_data: Dict) -> bool:
        """
        Process current market conditions and make autonomous trading decisions

        Returns True if a trade was executed
        """
        # Reset daily counter
        if datetime.now().date() > self.last_reset:
            self.trades_executed_today = 0
            self.last_reset = datetime.now().date()

        # Check if we can make more trades today
        if self.trades_executed_today >= self.max_daily_trades:
            logger.warning(f"⚠️  Daily trade limit ({self.max_daily_trades}) reached")
            return False

        # Make trade decision
        trade_decision = self.decision_engine.analyze_market_for_trades(
            defcon_level, signal_score, crisis_description, market_data
        )

        if not trade_decision:
            return False

        # Execute if auto_execute enabled
        if self.auto_execute:
            logger.info("🤖 BROKER: Executing autonomous buy...")

            # Build alert for execution
            alert = {
                'defcon_level': trade_decision['defcon_level'],
                'signal_score': trade_decision['signal_score'],
                'crisis_type': trade_decision['crisis_type'],
                'assets': {
                    'primary_asset': trade_decision['assets']['primary'],
                    'secondary_asset': trade_decision['assets']['secondary'],
                    'tertiary_asset': trade_decision['assets']['tertiary'],
                    'primary_allocation_pct': 0.50,
                    'secondary_allocation_pct': 0.30,
                    'tertiary_allocation_pct': 0.20,
                    'primary_size': trade_decision['position_size'] * 0.50,
                    'secondary_size': trade_decision['position_size'] * 0.30,
                    'tertiary_size': trade_decision['position_size'] * 0.20
                },
                'total_position_size': trade_decision['position_size'],
                'vix': trade_decision['vix'],
                'rationale': trade_decision['rationale'],
                'confidence_score': trade_decision['confidence'],
                'crisis_description': trade_decision.get('rationale', 'Autonomous broker decision'),
                'risk_reward_analysis': '',
                'time_window_minutes': 15
            }

            # Execute the trade
            trade_ids = self.decision_engine.paper_trading.execute_trade_package(alert, user_approval=True)

            if trade_ids:
                self.trades_executed_today += 1
                self.notification_engine.send_buy_notification(trade_decision)
                self.decision_engine.record_decision(trade_decision, executed=True, result="EXECUTED")

                # Send tips
                self._send_market_tips(defcon_level, signal_score, trade_decision)

                return True
        else:
            self.decision_engine.record_decision(trade_decision, executed=False)
            logger.info("ℹ️  Trade decision ready (auto_execute disabled)")

        return False

    def process_exits(self) -> int:
        """
        Process all open positions and execute exits if conditions met

        Returns number of exits executed
        """
        exits_executed = 0

        exit_decisions = self.decision_engine.analyze_positions_for_exits()

        for exit in exit_decisions:
            if self.auto_execute:
                logger.info(f"🤖 BROKER: Executing autonomous sell ({exit['asset_symbol']})...")

                # Execute the exit
                success = self.decision_engine.paper_trading.exit_position(
                    exit['trade_id'],
                    exit['decision_type'].split('_')[1].lower(),  # Extract reason
                    exit['current_price']
                )

                if success:
                    exits_executed += 1
                    self.notification_engine.send_sell_notification(exit)
                    self.decision_engine.record_decision(exit, executed=True, result="SOLD")

        return exits_executed

    def _send_market_tips(self, defcon_level: int, signal_score: float, decision: Dict):
        """Send helpful trading tips based on market conditions"""
        tips = []

        if signal_score > 80:
            tips.append("💡 Strong signal detected - this is a high-confidence setup")

        if defcon_level == 1:
            tips.append("🚨 DEFCON 1 reached - maximum market stress, positions sized down")

        performance = self._get_performance_tips()
        if performance:
            tips.append(performance)

        for tip in tips:
            logger.info(tip)

    def _get_performance_tips(self) -> Optional[str]:
        """Get performance-based tips"""
        perf = self.decision_engine.paper_trading.get_portfolio_performance()

        if perf['closed_trades'] > 5:
            if perf['win_rate'] > 60:
                return "📈 Excellent win rate (>60%) - system is performing well"
            elif perf['win_rate'] < 40:
                return "📉 Low win rate (<40%) - consider adjusting strategy"

        return None

    def get_status(self) -> Dict:
        """Get current broker status"""
        return {
            'auto_execute': self.auto_execute,
            'trades_today': self.trades_executed_today,
            'daily_limit': self.max_daily_trades,
            'can_trade': self.trades_executed_today < self.max_daily_trades,
            'decision_history_size': len(self.decision_engine.decision_history)
        }


def main():
    """Test broker system"""
    import logging as log
    log.basicConfig(level=log.INFO, format='%(levelname)s: %(message)s')

    print("\n" + "="*70)
    print("AUTONOMOUS BROKER AGENT - TEST")
    print("="*70)

    # Initialize broker
    broker = AutonomousBroker(auto_execute=False)  # Start with auto_execute=False for testing

    # Test buy decision
    print("\n📊 Testing Buy Decision...")
    test_market_data = {'vix': 25.0}
    buy_result = broker.process_market_conditions(
        defcon_level=2,
        signal_score=75.0,
        crisis_description="Tariff announcement and supply chain concerns",
        market_data=test_market_data
    )
    print(f"Buy Decision Result: {buy_result}")

    # Test exit detection
    print("\n🔍 Testing Exit Detection...")
    exits = broker.process_exits()
    print(f"Exits Detected: {exits}")

    # Get status
    status = broker.get_status()
    print(f"\nBroker Status: {status}")

    print("\n" + "="*70 + "\n")


if __name__ == '__main__':
    main()
