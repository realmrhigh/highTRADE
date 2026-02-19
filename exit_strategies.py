#!/usr/bin/env python3
"""
Enhanced Exit Strategies for Paper Trading
Implements multiple exit conditions beyond simple profit target and stop loss
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ExitSignal:
    """Represents an exit recommendation for a trade"""
    trade_id: int
    asset_symbol: str
    reason: str
    entry_price: float
    exit_price: float
    profit_loss_pct: float
    message: str
    priority: int  # Higher = more urgent (1-5)


class ExitStrategyManager:
    """Manages multiple exit strategies for open positions"""

    def __init__(self, 
                 profit_target: float = 0.05,
                 stop_loss: float = -0.03,
                 trailing_stop_pct: float = 0.02,
                 max_hold_hours: int = 72,
                 min_hold_hours: int = 1):
        """
        Args:
            profit_target: Fixed profit target (0.05 = 5%)
            stop_loss: Fixed stop loss (-0.03 = -3%)
            trailing_stop_pct: Trailing stop distance (0.02 = 2%)
            max_hold_hours: Exit position after this many hours
            min_hold_hours: Minimum hold time before allowing exits
        """
        self.profit_target = profit_target
        self.stop_loss = stop_loss
        self.trailing_stop_pct = trailing_stop_pct
        self.max_hold_hours = max_hold_hours
        self.min_hold_hours = min_hold_hours

        # Track highest price seen for each trade (for trailing stop)
        self.highest_prices: Dict[int, float] = {}

    def update_trailing_stop(self, trade_id: int, current_price: float):
        """Update the highest price seen for trailing stop calculation"""
        if trade_id not in self.highest_prices or current_price > self.highest_prices[trade_id]:
            self.highest_prices[trade_id] = current_price

    def check_trailing_stop(self, trade_id: int, entry_price: float, 
                           current_price: float, min_hold_met: bool) -> Optional[ExitSignal]:
        """
        Check if trailing stop loss is hit
        
        Trailing stop only activates after position is profitable
        """
        if not min_hold_met:
            return None

        # Update highest price
        self.update_trailing_stop(trade_id, current_price)

        highest_price = self.highest_prices.get(trade_id, entry_price)

        # Only use trailing stop if we're profitable
        if highest_price <= entry_price:
            return None

        # Calculate drawdown from peak
        drawdown = (current_price - highest_price) / highest_price

        # If price drops by trailing_stop_pct from peak, exit
        if drawdown <= -self.trailing_stop_pct:
            profit_pct = (current_price - entry_price) / entry_price
            return ExitSignal(
                trade_id=trade_id,
                asset_symbol="",  # Set by caller
                reason="trailing_stop",
                entry_price=entry_price,
                exit_price=current_price,
                profit_loss_pct=profit_pct,
                message=f"📉 TRAILING STOP: Down {abs(drawdown)*100:.1f}% from peak ${highest_price:.2f}",
                priority=3
            )

        return None

    def check_profit_target(self, entry_price: float, current_price: float, 
                           min_hold_met: bool) -> Optional[float]:
        """Check if profit target is hit"""
        if not min_hold_met:
            return None

        profit_pct = (current_price - entry_price) / entry_price
        if profit_pct >= self.profit_target:
            return profit_pct
        return None

    def check_stop_loss(self, entry_price: float, current_price: float,
                       min_hold_met: bool) -> Optional[float]:
        """Check if stop loss is hit — ALWAYS fires regardless of min_hold_hours.
        Stop-loss is a safety mechanism that must never be gated by hold time."""
        loss_pct = (current_price - entry_price) / entry_price
        if loss_pct <= self.stop_loss:
            return loss_pct
        return None

    def check_time_based_exit(self, entry_datetime: datetime, 
                              current_price: float, entry_price: float,
                              holding_hours: float) -> Optional[ExitSignal]:
        """
        Check for time-based exit conditions
        
        1. Max hold time exceeded → exit regardless of P&L
        2. Approaching max hold + position is red → exit to prevent prolonged loss
        """
        profit_pct = (current_price - entry_price) / entry_price

        # Max hold time exceeded
        if holding_hours >= self.max_hold_hours:
            return ExitSignal(
                trade_id=0,  # Set by caller
                asset_symbol="",
                reason="time_limit",
                entry_price=entry_price,
                exit_price=current_price,
                profit_loss_pct=profit_pct,
                message=f"⏰ TIME LIMIT: Held {holding_hours:.1f}h (max {self.max_hold_hours}h)",
                priority=2
            )

        # Approaching max hold time and losing money
        if holding_hours >= (self.max_hold_hours * 0.8) and profit_pct < 0:
            return ExitSignal(
                trade_id=0,
                asset_symbol="",
                reason="time_and_loss",
                entry_price=entry_price,
                exit_price=current_price,
                profit_loss_pct=profit_pct,
                message=f"⏰ TIME & LOSS: Held {holding_hours:.1f}h and {profit_pct*100:.2f}% red",
                priority=3
            )

        return None

    def check_defcon_reversion(self, entry_defcon: int, current_defcon: int,
                               entry_price: float, current_price: float) -> Optional[ExitSignal]:
        """
        Check if DEFCON has reverted to safer levels
        
        If we entered at DEFCON 2/1 and it's now back to 3+, crisis may be over
        """
        if entry_defcon <= 2 and current_defcon >= 3:
            profit_pct = (current_price - entry_price) / entry_price
            return ExitSignal(
                trade_id=0,
                asset_symbol="",
                reason="defcon_revert",
                entry_price=entry_price,
                exit_price=current_price,
                profit_loss_pct=profit_pct,
                message=f"🟢 DEFCON REVERT: {entry_defcon} → {current_defcon} (crisis over)",
                priority=2
            )

        return None

    def evaluate_position(self, trade: Dict, current_price: float, 
                         current_defcon: int = 5) -> Optional[ExitSignal]:
        """
        Evaluate all exit strategies for a single position
        
        Args:
            trade: Dict with keys: trade_id, asset_symbol, entry_price, entry_date, 
                   entry_time, defcon_at_entry
            current_price: Current market price
            current_defcon: Current DEFCON level
            
        Returns:
            ExitSignal if any exit condition is met, else None
        """
        entry_price = trade['entry_price']
        trade_id = trade['trade_id']
        asset_symbol = trade['asset_symbol']

        # Calculate holding time
        entry_dt = datetime.strptime(f"{trade['entry_date']} {trade['entry_time']}", '%Y-%m-%d %H:%M:%S')
        holding_hours = (datetime.now() - entry_dt).total_seconds() / 3600
        min_hold_met = holding_hours >= self.min_hold_hours

        # Priority order: Check from most important to least
        # 1. Stop loss (highest priority - prevent catastrophic losses)
        loss_pct = self.check_stop_loss(entry_price, current_price, min_hold_met)
        if loss_pct is not None:
            return ExitSignal(
                trade_id=trade_id,
                asset_symbol=asset_symbol,
                reason="stop_loss",
                entry_price=entry_price,
                exit_price=current_price,
                profit_loss_pct=loss_pct,
                message=f"⚠️  STOP LOSS: {asset_symbol} {loss_pct*100:.2f}%",
                priority=5
            )

        # 2. Profit target (lock in gains)
        profit_pct = self.check_profit_target(entry_price, current_price, min_hold_met)
        if profit_pct is not None:
            return ExitSignal(
                trade_id=trade_id,
                asset_symbol=asset_symbol,
                reason="profit_target",
                entry_price=entry_price,
                exit_price=current_price,
                profit_loss_pct=profit_pct,
                message=f"✅ PROFIT TARGET: {asset_symbol} +{profit_pct*100:.2f}%",
                priority=4
            )

        # 3. Trailing stop (protect profits)
        trailing_signal = self.check_trailing_stop(trade_id, entry_price, current_price, min_hold_met)
        if trailing_signal:
            trailing_signal.asset_symbol = asset_symbol
            trailing_signal.trade_id = trade_id
            return trailing_signal

        # 4. DEFCON reversion (crisis over)
        defcon_signal = self.check_defcon_reversion(
            trade['defcon_at_entry'], current_defcon, entry_price, current_price
        )
        if defcon_signal:
            defcon_signal.trade_id = trade_id
            defcon_signal.asset_symbol = asset_symbol
            return defcon_signal

        # 5. Time-based exit (last resort)
        time_signal = self.check_time_based_exit(entry_dt, current_price, entry_price, holding_hours)
        if time_signal:
            time_signal.trade_id = trade_id
            time_signal.asset_symbol = asset_symbol
            return time_signal

        return None

    def reset_trailing_stop(self, trade_id: int):
        """Reset trailing stop for a trade (used when trade is closed)"""
        if trade_id in self.highest_prices:
            del self.highest_prices[trade_id]

    def get_stats(self) -> Dict:
        """Get statistics about exit strategy usage"""
        return {
            'profit_target': f"+{self.profit_target*100:.1f}%",
            'stop_loss': f"{self.stop_loss*100:.1f}%",
            'trailing_stop': f"{self.trailing_stop_pct*100:.1f}%",
            'max_hold_hours': self.max_hold_hours,
            'min_hold_hours': self.min_hold_hours,
            'tracked_positions': len(self.highest_prices)
        }


# Standalone test
if __name__ == '__main__':
    print("Testing Exit Strategy Manager...")
    print("=" * 60)

    manager = ExitStrategyManager(
        profit_target=0.05,
        stop_loss=-0.03,
        trailing_stop_pct=0.02,
        max_hold_hours=72,
        min_hold_hours=1
    )

    # Test case 1: Profit target
    print("\n1. Testing profit target (+5%):")
    trade1 = {
        'trade_id': 1,
        'asset_symbol': 'AAPL',
        'entry_price': 100.0,
        'entry_date': '2026-02-10',
        'entry_time': '09:30:00',
        'defcon_at_entry': 2
    }
    signal = manager.evaluate_position(trade1, current_price=105.5, current_defcon=2)
    if signal:
        print(f"   ✓ {signal.message}")
    else:
        print("   No exit signal")

    # Test case 2: Trailing stop
    print("\n2. Testing trailing stop (rose to 110, now at 107):")
    trade2 = {
        'trade_id': 2,
        'asset_symbol': 'TSLA',
        'entry_price': 100.0,
        'entry_date': '2026-02-12',
        'entry_time': '10:00:00',
        'defcon_at_entry': 1
    }
    # Simulate price rising
    manager.update_trailing_stop(2, 110.0)
    # Then falling 3% from peak
    signal = manager.evaluate_position(trade2, current_price=106.8, current_defcon=1)
    if signal:
        print(f"   ✓ {signal.message}")
    else:
        print("   No exit signal")

    # Test case 3: Time limit
    print("\n3. Testing time limit (held 75 hours, max 72):")
    trade3 = {
        'trade_id': 3,
        'asset_symbol': 'NVDA',
        'entry_price': 100.0,
        'entry_date': '2026-02-11',
        'entry_time': '09:00:00',
        'defcon_at_entry': 2
    }
    signal = manager.evaluate_position(trade3, current_price=102.0, current_defcon=2)
    if signal:
        print(f"   ✓ {signal.message}")
    else:
        print("   No exit signal")

    print("\n" + "=" * 60)
    print("Stats:", manager.get_stats())
