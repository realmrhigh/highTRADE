#!/usr/bin/env python3
"""
Test suite for paper trading system
"""

from paper_trading import PaperTradingEngine
import json

def test_crisis_analysis():
    """Test crisis type analysis"""
    engine = PaperTradingEngine()

    # Test 1: Geopolitical Trade War Crisis
    print("\n" + "="*70)
    print("TEST 1: Geopolitical Trade War Crisis")
    print("="*70)
    alert1 = engine.generate_trade_alert(
        defcon_level=2,
        signal_score=75.0,
        crisis_description='Tariff announcement causing supply chain disruptions and trade tensions',
        market_data={'vix': 25.5}
    )
    print(f"Crisis Type: {alert1['crisis_type']}")
    print(f"Primary: {alert1['assets']['primary_asset']} | Secondary: {alert1['assets']['secondary_asset']} | Tertiary: {alert1['assets']['tertiary_asset']}")
    print(f"Confidence: {alert1['confidence_score']}/100")
    print(f"Position Size: ${alert1['total_position_size']:,.0f}")
    print()

    # Test 2: Pandemic scenario
    print("TEST 2: Health Crisis / Pandemic")
    print("="*70)
    alert2 = engine.generate_trade_alert(
        defcon_level=1,
        signal_score=85.0,
        crisis_description='COVID-19 pandemic escalation, lockdowns announced, health crisis spreading',
        market_data={'vix': 45.0}
    )
    print(f"Crisis Type: {alert2['crisis_type']}")
    print(f"Primary: {alert2['assets']['primary_asset']} | Secondary: {alert2['assets']['secondary_asset']} | Tertiary: {alert2['assets']['tertiary_asset']}")
    print(f"Confidence: {alert2['confidence_score']}/100")
    print(f"Position Size: ${alert2['total_position_size']:,.0f}")
    print()

    # Test 3: Market correction
    print("TEST 3: Generic Market Correction")
    print("="*70)
    alert3 = engine.generate_trade_alert(
        defcon_level=3,
        signal_score=55.0,
        crisis_description='Market drawdown, S&P down 5%, volatility spike',
        market_data={'vix': 30.0}
    )
    print(f"Crisis Type: {alert3['crisis_type']}")
    print(f"Primary: {alert3['assets']['primary_asset']} | Secondary: {alert3['assets']['secondary_asset']} | Tertiary: {alert3['assets']['tertiary_asset']}")
    print(f"Confidence: {alert3['confidence_score']}/100")
    print(f"Position Size: ${alert3['total_position_size']:,.0f}")
    print()

    # Test 4: Liquidity/Credit crisis
    print("TEST 4: Liquidity / Credit Crisis")
    print("="*70)
    alert4 = engine.generate_trade_alert(
        defcon_level=2,
        signal_score=80.0,
        crisis_description='Credit spreads widening, financial stress signals, banking concerns',
        market_data={'vix': 35.0}
    )
    print(f"Crisis Type: {alert4['crisis_type']}")
    print(f"Primary: {alert4['assets']['primary_asset']} | Secondary: {alert4['assets']['secondary_asset']} | Tertiary: {alert4['assets']['tertiary_asset']}")
    print(f"Confidence: {alert4['confidence_score']}/100")
    print(f"Position Size: ${alert4['total_position_size']:,.0f}")
    print()


def test_position_sizing():
    """Test VIX-based position sizing"""
    engine = PaperTradingEngine()

    print("\nTEST 5: Position Sizing at Different VIX Levels")
    print("="*70)
    print(f"{'VIX':<6} {'Position Size':<20} {'Change vs Base':<20}")
    print("-"*70)

    base_size = engine.calculate_position_size_vix_adjusted(20.0)

    for vix in [10, 15, 20, 30, 40, 60, 80]:
        size = engine.calculate_position_size_vix_adjusted(vix)
        change_pct = ((size - base_size) / base_size) * 100
        print(f"{vix:<6} ${size:>15,.0f}  {change_pct:>15.1f}%")

    print()


def test_trade_execution():
    """Test trade execution and portfolio tracking"""
    engine = PaperTradingEngine()

    print("\nTEST 6: Trade Execution and P&L Tracking")
    print("="*70)

    # Generate an alert
    alert = engine.generate_trade_alert(
        defcon_level=2,
        signal_score=75.0,
        crisis_description='Test trade execution',
        market_data={'vix': 20.0}
    )

    # Execute the trade
    print(f"Executing trade package: {alert['assets']['primary_asset']}, "
          f"{alert['assets']['secondary_asset']}, {alert['assets']['tertiary_asset']}")

    trade_ids = engine.execute_trade_package(alert, user_approval=True)
    print(f"Executed trades: {trade_ids}")

    # Get portfolio performance
    perf = engine.get_portfolio_performance()
    print(f"\nPortfolio After Execution:")
    print(f"  Total Trades: {perf['total_trades']}")
    print(f"  Open Trades: {perf['open_trades']}")
    print(f"  Total P&L: ${perf['total_profit_loss_dollars']:,.0f}")

    # Get open positions
    open_pos = engine.get_open_positions()
    print(f"\nOpen Positions ({len(open_pos)}):")
    for pos in open_pos:
        print(f"  • {pos['asset_symbol']}: {pos['shares']} shares @ ${pos['entry_price']:.2f}")

    print()


if __name__ == '__main__':
    print("\n" + "="*70)
    print("PAPER TRADING ENGINE - TEST SUITE")
    print("="*70)

    test_crisis_analysis()
    test_position_sizing()
    test_trade_execution()

    print("="*70)
    print("All tests completed successfully!")
    print("="*70)
