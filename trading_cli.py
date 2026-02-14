#!/usr/bin/env python3
"""
HighTrade Paper Trading CLI - Interactive trading command interface
Provides commands to manage pending trades, execute exits, and view portfolio status
"""

import sys
import logging
from pathlib import Path
from paper_trading import PaperTradingEngine
from portfolio_dashboard import PortfolioDashboard
from queries import TradeDataQuery
from quick_money_research import QuickMoneyResearch

DB_PATH = Path.home() / 'trading_data' / 'trading_history.db'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class TradingCLI:
    """Interactive CLI for paper trading management"""

    def __init__(self):
        self.engine = PaperTradingEngine(DB_PATH)
        self.dashboard = PortfolioDashboard(DB_PATH)
        self.query = TradeDataQuery(DB_PATH)
        self.quick_money = QuickMoneyResearch()
        self.running = True

    def print_menu(self):
        """Print main menu"""
        print("\n" + "="*70)
        print("HighTrade Paper Trading CLI")
        print("="*70)
        print("""
Commands:
  status              - Show current portfolio status
  positions           - List all open positions
  performance         - Show performance by asset and crisis type
  allocation          - Show current asset allocation
  recent-trades       - Show recently closed trades
  quick-money         - Research quick flip opportunities (NEW!)
  execute-trades      - Execute pending trade alerts (from orchestrator)
  execute-exits       - Execute pending exit signals
  help                - Show this menu
  quit                - Exit the program
        """)

    def show_portfolio_status(self):
        """Display portfolio status"""
        perf = self.engine.get_portfolio_performance()
        open_pos = self.engine.get_open_positions()

        print("\n" + "="*70)
        print("📊 PORTFOLIO STATUS")
        print("="*70)

        print("\nSummary:")
        print(f"  Total Trades:     {perf['total_trades']}")
        print(f"  Open Trades:      {perf['open_trades']}")
        print(f"  Closed Trades:    {perf['closed_trades']}")
        print(f"  Winning Trades:   {perf.get('winning_trades', 0)}")
        print(f"  Losing Trades:    {perf.get('losing_trades', 0)}")

        if perf['closed_trades'] > 0:
            print(f"\nPerformance:")
            pnl_color = "✅" if perf['total_profit_loss_dollars'] >= 0 else "❌"
            print(f"  Total P&L:        {pnl_color} ${perf['total_profit_loss_dollars']:+,.0f} "
                  f"({perf['total_profit_loss_percent']:+.2f}%)")
            print(f"  Win Rate:         {perf['win_rate']:.1f}%")
            print(f"  Profit Factor:    {perf['profit_factor']:.2f}")

        if open_pos:
            print(f"\nOpen Positions ({len(open_pos)}):")
            total_open_value = 0
            for pos in open_pos:
                print(f"  • {pos['asset_symbol']:5} - {pos['shares']:3d} shares @ ${pos['entry_price']:7.2f} "
                      f"(${pos['position_size_dollars']:8,.0f})")
                total_open_value += pos['position_size_dollars']
            print(f"  {'Total Open Value':>30} ${total_open_value:>10,.0f}")

        print("="*70 + "\n")

    def show_positions(self):
        """Show detailed open positions"""
        open_pos = self.engine.get_open_positions()

        print("\n" + "="*70)
        print("📍 OPEN POSITIONS")
        print("="*70)

        if not open_pos:
            print("No open positions")
        else:
            print(f"\n{'ID':<6} {'Asset':<8} {'Shares':<8} {'Entry Price':<15} {'Value':<15} {'Entry Date':<12}")
            print("-"*70)
            for pos in open_pos:
                print(f"{pos['trade_id']:<6} {pos['asset_symbol']:<8} {pos['shares']:<8} "
                      f"${pos['entry_price']:<14.2f} ${pos['position_size_dollars']:<14,.0f} "
                      f"{pos['entry_date']:<12}")

        print("="*70 + "\n")

    def show_performance(self):
        """Show performance by asset and crisis type"""
        by_asset = self.dashboard.get_performance_by_asset()
        by_crisis = self.dashboard.get_performance_by_crisis_type()

        print("\n" + "="*70)
        print("📈 PERFORMANCE ANALYSIS")
        print("="*70)

        print("\nBy Asset:")
        if not by_asset:
            print("  No trades yet")
        else:
            print(f"  {'Asset':<10} {'Trades':<8} {'P&L':<15} {'Win %':<10} {'Avg Return':<12}")
            print("  " + "-"*65)
            for asset in sorted(by_asset.keys()):
                metrics = by_asset[asset]
                pnl_str = f"${metrics['total_pnl']:+,.0f}"
                print(f"  {asset:<10} {metrics['total_trades']:<8} {pnl_str:<15} "
                      f"{metrics['win_rate']:.0f}%{' ':<7} {metrics['avg_return']:+.2f}%")

        print("\nBy Crisis Type:")
        if not by_crisis:
            print("  No trades yet")
        else:
            print(f"  {'Crisis Type':<15} {'Trades':<8} {'P&L':<15} {'Win %':<10} {'Avg Return':<12}")
            print("  " + "-"*65)
            for crisis in sorted(by_crisis.keys()):
                metrics = by_crisis[crisis]
                pnl_str = f"${metrics['total_pnl']:+,.0f}"
                print(f"  {crisis:<15} {metrics['total_trades']:<8} {pnl_str:<15} "
                      f"{metrics['win_rate']:.0f}%{' ':<7} {metrics['avg_return']:+.2f}%")

        print("="*70 + "\n")

    def show_allocation(self):
        """Show asset allocation"""
        allocation = self.dashboard.get_asset_allocation()

        print("\n" + "="*70)
        print("🎯 ASSET ALLOCATION (Open Positions)")
        print("="*70)

        total_value = allocation['total_value']

        if not allocation['allocations']:
            print("No open positions")
        else:
            print(f"\n{'Asset':<10} {'Positions':<12} {'Value':<15} {'Allocation':<15}")
            print("-"*70)
            for asset in sorted(allocation['allocations'].keys()):
                data = allocation['allocations'][asset]
                value_str = f"${data['total_value']:,.0f}"
                alloc_str = f"{data['allocation_pct']:.1f}%"
                print(f"{asset:<10} {data['position_count']:<12} {value_str:<15} {alloc_str:<15}")

            print("-"*70)
            print(f"{'TOTAL':<10} {'':<12} ${total_value:>13,.0f}")

        print("="*70 + "\n")

    def show_recent_trades(self):
        """Show recently closed trades"""
        recent = self.dashboard.get_recent_trades(limit=20)

        print("\n" + "="*70)
        print("📋 RECENT CLOSED TRADES")
        print("="*70)

        if not recent:
            print("No closed trades yet")
        else:
            print(f"\n{'Asset':<8} {'Entry':<10} {'Exit':<10} {'P&L':<15} {'Exit Reason':<20} {'Hours':<8}")
            print("-"*80)
            for trade in recent:
                pnl_str = f"${trade['profit_loss_dollars']:+,.0f}"
                print(f"{trade['asset_symbol']:<8} ${trade['entry_price']:<9.2f} "
                      f"${trade['exit_price']:<9.2f} {pnl_str:<15} {trade['exit_reason']:<20} "
                      f"{trade['holding_hours']:<8.1f}")

        print("="*70 + "\n")

    def execute_trades_interactive(self):
        """Interactive trade execution"""
        print("\n" + "="*70)
        print("⚙️  TRADE EXECUTION")
        print("="*70)
        print("\nNote: To execute trades, pending alerts must be available from the orchestrator.")
        print("Please run 'python hightrade_orchestrator.py test' to generate alerts.")
        print("\nFor now, you can manually manage existing positions.")

    def execute_exits_interactive(self):
        """Interactive exit execution"""
        print("\n" + "="*70)
        print("🚪 EXIT MANAGEMENT")
        print("="*70)
        print("\nNote: Exits are detected automatically by the monitoring system.")
        print("When positions hit profit targets (+5%) or stop loss (-3%), they are marked for exit.")

    def research_quick_money(self):
        """Research quick flip opportunities"""
        print("\n" + "="*70)
        print("💰 QUICK MONEY RESEARCH - Finding Quick Flip Opportunities")
        print("="*70)
        print("\nAnalyzing stocks with high volatility and momentum...")
        print("This may take a minute...\n")
        
        try:
            # Run the research
            opportunities = self.quick_money.research_quick_flip_opportunities()
            
            if not opportunities:
                print("\n❌ No quick flip opportunities found at this time.")
                print("Try again later when market volatility increases.")
                return
            
            # Display the report
            self.quick_money.print_opportunities_report()
            
            # Ask if user wants to record any for tracking
            print("\n💡 Would you like to record any of these opportunities for tracking?")
            response = input("Enter symbol(s) to track (comma separated), or press Enter to skip: ").strip().upper()
            
            if response:
                symbols_to_track = [s.strip() for s in response.split(',')]
                for symbol in symbols_to_track:
                    # Find the opportunity
                    opp = next((o for o in opportunities if o['symbol'] == symbol), None)
                    if opp:
                        self.quick_money.record_trade_to_db(opp, action='tracking')
                        print(f"✅ Recording {symbol} for tracking")
                    else:
                        print(f"⚠️  {symbol} not found in opportunities list")
                        
        except Exception as e:
            logger.error(f"Error during quick money research: {e}")
            print(f"\n❌ Error: {e}")
            print("Check your internet connection and try again.")

    def run(self):
        """Run the CLI"""
        print("\n" + "="*70)
        print("HighTrade Paper Trading CLI")
        print("="*70)
        print("\nType 'help' to see available commands")

        while self.running:
            try:
                command = input("\nhightrade> ").strip().lower()

                if command == 'status':
                    self.show_portfolio_status()
                elif command == 'positions':
                    self.show_positions()
                elif command == 'performance':
                    self.show_performance()
                elif command == 'allocation':
                    self.show_allocation()
                elif command == 'recent-trades':
                    self.show_recent_trades()
                elif command == 'quick-money':
                    self.research_quick_money()
                elif command == 'execute-trades':
                    self.execute_trades_interactive()
                elif command == 'execute-exits':
                    self.execute_exits_interactive()
                elif command in ['help', '?']:
                    self.print_menu()
                elif command in ['quit', 'exit', 'q']:
                    print("Goodbye!")
                    self.running = False
                elif command == '':
                    continue
                else:
                    print(f"Unknown command: {command}")
                    print("Type 'help' for available commands")

            except KeyboardInterrupt:
                print("\nGoodbye!")
                self.running = False
            except Exception as e:
                print(f"Error: {e}")


def main():
    """Main entry point"""
    import argparse

    parser = argparse.ArgumentParser(
        description='HighTrade Paper Trading CLI',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Start interactive CLI
  python3 trading_cli.py

  # Show quick status and exit
  python3 trading_cli.py status

  # Show portfolio performance
  python3 trading_cli.py performance
        """
    )

    parser.add_argument(
        'command',
        nargs='?',
        default='interactive',
        choices=['interactive', 'status', 'positions', 'performance', 'allocation', 'recent-trades', 'quick-money'],
        help='Command to execute'
    )

    args = parser.parse_args()
    cli = TradingCLI()

    if args.command == 'interactive':
        cli.run()
    elif args.command == 'status':
        cli.show_portfolio_status()
    elif args.command == 'positions':
        cli.show_positions()
    elif args.command == 'performance':
        cli.show_performance()
    elif args.command == 'allocation':
        cli.show_allocation()
    elif args.command == 'recent-trades':
        cli.show_recent_trades()
    elif args.command == 'quick-money':
        cli.research_quick_money()


if __name__ == '__main__':
    main()
