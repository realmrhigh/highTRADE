#!/usr/bin/env python3
"""
HighTrade Database Query Module
Provides easy-to-use query functions for Cowork integration
"""

import sqlite3
import json
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

DB_PATH = Path.home() / 'trading_data' / 'trading_history.db'

class TradeDataQuery:
    """Query interface for HighTrade database"""

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path

    def connect(self):
        self.conn = sqlite3.connect(str(self.db_path))
        self.cursor = self.conn.cursor()
        self.cursor.row_factory = sqlite3.Row

    def disconnect(self):
        self.conn.close()

    def query_crisis_by_name(self, name: str) -> Dict[str, Any]:
        """Retrieve crisis event by name"""
        self.connect()
        try:
            self.cursor.execute('''
            SELECT * FROM crisis_events WHERE name = ?
            ''', (name,))
            row = self.cursor.fetchone()
            return dict(row) if row else None
        finally:
            self.disconnect()

    def query_all_crises(self) -> List[Dict[str, Any]]:
        """Get all historical crises"""
        self.connect()
        try:
            self.cursor.execute('''
            SELECT crisis_id, name, category, severity, start_date, crisis_bottom_date,
                   market_drop_percent, recovery_percent, recovery_days
            FROM crisis_events
            ORDER BY start_date DESC
            ''')
            return [dict(row) for row in self.cursor.fetchall()]
        finally:
            self.disconnect()

    def query_crisis_signals(self, crisis_id: int) -> List[Dict[str, Any]]:
        """Get all signals for a specific crisis"""
        self.connect()
        try:
            self.cursor.execute('''
            SELECT signal_id, signal_type, signal_weight, detected_date,
                   detected_time, value, description
            FROM signals
            WHERE crisis_id = ?
            ORDER BY detected_date ASC, detected_time ASC
            ''', (crisis_id,))
            return [dict(row) for row in self.cursor.fetchall()]
        finally:
            self.disconnect()

    def query_defcon_status(self) -> Dict[str, Any]:
        """Get current DEFCON status"""
        self.connect()
        try:
            self.cursor.execute('''
            SELECT monitoring_date, monitoring_time, bond_10yr_yield,
                   vix_close, defcon_level, signal_score
            FROM signal_monitoring
            ORDER BY monitoring_date DESC, monitoring_time DESC
            LIMIT 1
            ''')
            row = self.cursor.fetchone()
            if row:
                return {
                    'date': row['monitoring_date'],
                    'time': row['monitoring_time'],
                    'bond_yield_percent': row['bond_10yr_yield'],
                    'vix': row['vix_close'],
                    'defcon_level': row['defcon_level'],
                    'signal_score': row['signal_score'],
                    'defcon_status': self._defcon_description(row['defcon_level'])
                }
            return None
        finally:
            self.disconnect()

    def query_monitoring_history(self, days: int = 7) -> List[Dict[str, Any]]:
        """Get monitoring history for the past N days"""
        self.connect()
        try:
            self.cursor.execute('''
            SELECT monitoring_date, monitoring_time, bond_10yr_yield,
                   vix_close, defcon_level, signal_score
            FROM signal_monitoring
            WHERE monitoring_date >= date('now', ?)
            ORDER BY monitoring_date DESC, monitoring_time DESC
            ''', (f'-{days} days',))
            return [dict(row) for row in self.cursor.fetchall()]
        finally:
            self.disconnect()

    def query_defcon_escalations(self, days: int = 7) -> List[Dict[str, Any]]:
        """Get DEFCON level changes in the past N days"""
        self.connect()
        try:
            self.cursor.execute('''
            SELECT event_date, event_time, defcon_level, reason,
                   contributing_signals, signal_score
            FROM defcon_history
            WHERE event_date >= date('now', ?)
            ORDER BY event_date DESC, event_time DESC
            ''', (f'-{days} days',))
            return [dict(row) for row in self.cursor.fetchall()]
        finally:
            self.disconnect()

    def query_trades_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent trade history"""
        self.connect()
        try:
            self.cursor.execute('''
            SELECT t.trade_id, t.asset_symbol, t.entry_date, t.entry_price, t.exit_date,
                   t.exit_price, t.profit_loss_percent, t.holding_hours, t.status,
                   c.name as crisis_name
            FROM trade_records t
            LEFT JOIN crisis_events c ON t.crisis_id = c.crisis_id
            ORDER BY t.entry_date DESC
            LIMIT ?
            ''', (limit,))
            return [dict(row) for row in self.cursor.fetchall()]
        finally:
            self.disconnect()

    def query_open_positions(self) -> List[Dict[str, Any]]:
        """Get all currently open trades"""
        self.connect()
        try:
            self.cursor.execute('''
            SELECT trade_id, asset_symbol, entry_date, entry_price, shares,
                   position_size_dollars, defcon_at_entry
            FROM trade_records
            WHERE status = 'open'
            ORDER BY entry_date DESC
            ''')
            return [dict(row) for row in self.cursor.fetchall()]
        finally:
            self.disconnect()

    def query_portfolio_pnl(self) -> Dict[str, Any]:
        """Get total portfolio P&L"""
        self.connect()
        try:
            self.cursor.execute('''
            SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) as open_trades,
                SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) as closed_trades,
                SUM(CASE WHEN status = 'closed' THEN profit_loss_dollars ELSE 0 END) as total_pnl,
                SUM(CASE WHEN status = 'closed' AND profit_loss_dollars > 0 THEN 1 ELSE 0 END) as winning_trades,
                SUM(CASE WHEN status = 'closed' AND profit_loss_dollars <= 0 THEN 1 ELSE 0 END) as losing_trades,
                AVG(CASE WHEN status = 'closed' THEN profit_loss_percent ELSE NULL END) as avg_return_pct
            FROM trade_records
            ''')
            row = self.cursor.fetchone()
            if row:
                return {
                    'total_trades': row['total_trades'],
                    'open_trades': row['open_trades'],
                    'closed_trades': row['closed_trades'],
                    'total_pnl': row['total_pnl'],
                    'winning_trades': row['winning_trades'],
                    'losing_trades': row['losing_trades'],
                    'avg_return_pct': round(row['avg_return_pct'], 2) if row['avg_return_pct'] else 0,
                    'win_rate': round((row['winning_trades'] / row['closed_trades'] * 100), 1) if row['closed_trades'] > 0 else 0
                }
            return {}
        finally:
            self.disconnect()

    def query_performance_by_asset(self) -> Dict[str, Any]:
        """Get performance metrics by asset"""
        self.connect()
        try:
            self.cursor.execute('''
            SELECT
                asset_symbol,
                COUNT(*) as total_trades,
                SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) as closed_trades,
                SUM(CASE WHEN status = 'closed' AND profit_loss_dollars > 0 THEN 1 ELSE 0 END) as winners,
                SUM(CASE WHEN status = 'closed' THEN profit_loss_dollars ELSE 0 END) as total_pnl,
                AVG(CASE WHEN status = 'closed' THEN profit_loss_percent ELSE NULL END) as avg_return_pct
            FROM trade_records
            GROUP BY asset_symbol
            ORDER BY total_pnl DESC
            ''')
            result = {}
            for row in self.cursor.fetchall():
                row = dict(row)
                win_rate = (row['winners'] / row['closed_trades'] * 100) if row['closed_trades'] > 0 else 0
                result[row['asset_symbol']] = {
                    'total_trades': row['total_trades'],
                    'closed_trades': row['closed_trades'],
                    'winners': row['winners'],
                    'total_pnl': round(row['total_pnl'], 2) if row['total_pnl'] else 0,
                    'avg_return_pct': round(row['avg_return_pct'], 2) if row['avg_return_pct'] else 0,
                    'win_rate': round(win_rate, 1)
                }
            return result
        finally:
            self.disconnect()

    def query_performance_by_crisis_type(self) -> Dict[str, Any]:
        """Get performance metrics by crisis type"""
        self.connect()
        try:
            self.cursor.execute('''
            SELECT
                c.category,
                COUNT(t.trade_id) as total_trades,
                SUM(CASE WHEN t.status = 'closed' THEN 1 ELSE 0 END) as closed_trades,
                SUM(CASE WHEN t.status = 'closed' AND t.profit_loss_dollars > 0 THEN 1 ELSE 0 END) as winners,
                SUM(CASE WHEN t.status = 'closed' THEN t.profit_loss_dollars ELSE 0 END) as total_pnl,
                AVG(CASE WHEN t.status = 'closed' THEN t.profit_loss_percent ELSE NULL END) as avg_return_pct
            FROM trade_records t
            LEFT JOIN crisis_events c ON t.crisis_id = c.crisis_id
            GROUP BY c.category
            ORDER BY total_pnl DESC
            ''')
            result = {}
            for row in self.cursor.fetchall():
                row = dict(row)
                category = row['category'] or 'unknown'
                win_rate = (row['winners'] / row['closed_trades'] * 100) if row['closed_trades'] > 0 else 0
                result[category] = {
                    'total_trades': row['total_trades'],
                    'closed_trades': row['closed_trades'],
                    'winners': row['winners'],
                    'total_pnl': round(row['total_pnl'], 2) if row['total_pnl'] else 0,
                    'avg_return_pct': round(row['avg_return_pct'], 2) if row['avg_return_pct'] else 0,
                    'win_rate': round(win_rate, 1)
                }
            return result
        finally:
            self.disconnect()

    def query_asset_allocation(self) -> Dict[str, Any]:
        """Get current portfolio asset allocation (open positions only)"""
        self.connect()
        try:
            self.cursor.execute('''
            SELECT
                asset_symbol,
                COUNT(*) as position_count,
                SUM(position_size_dollars) as total_value,
                AVG(entry_price) as avg_entry_price
            FROM trade_records
            WHERE status = 'open'
            GROUP BY asset_symbol
            ORDER BY total_value DESC
            ''')
            result = {}
            total_value = 0
            rows = [dict(row) for row in self.cursor.fetchall()]

            for row in rows:
                total_value += row['total_value'] if row['total_value'] else 0

            for row in rows:
                allocation_pct = (row['total_value'] / total_value * 100) if total_value > 0 else 0
                result[row['asset_symbol']] = {
                    'position_count': row['position_count'],
                    'total_value': round(row['total_value'], 2) if row['total_value'] else 0,
                    'allocation_percent': round(allocation_pct, 1),
                    'avg_entry_price': round(row['avg_entry_price'], 2) if row['avg_entry_price'] else 0
                }

            return {
                'allocations': result,
                'total_portfolio_value': round(total_value, 2)
            }
        finally:
            self.disconnect()

    def query_crisis_statistics(self) -> Dict[str, Any]:
        """Get aggregate statistics about historical crises"""
        self.connect()
        try:
            self.cursor.execute('''
            SELECT
                COUNT(*) as total_crises,
                AVG(market_drop_percent) as avg_drop_percent,
                MIN(market_drop_percent) as worst_drop_percent,
                AVG(recovery_percent) as avg_recovery_percent,
                AVG(recovery_days) as avg_recovery_days
            FROM crisis_events
            ''')
            row = self.cursor.fetchone()
            if row:
                return {
                    'total_crises': row[0],
                    'avg_drop_percent': round(row[1], 1) if row[1] else None,
                    'worst_drop_percent': round(row[2], 1) if row[2] else None,
                    'avg_recovery_percent': round(row[3], 1) if row[3] else None,
                    'avg_recovery_days': round(row[4], 1) if row[4] else None
                }
            return None
        finally:
            self.disconnect()

    def query_similar_crises(self, market_drop_threshold: float = 5.0) -> List[Dict[str, Any]]:
        """Find crises with similar market impact"""
        self.connect()
        try:
            self.cursor.execute('''
            SELECT crisis_id, name, category, market_drop_percent,
                   recovery_percent, recovery_days, crisis_bottom_date
            FROM crisis_events
            WHERE ABS(market_drop_percent) >= ?
            ORDER BY market_drop_percent DESC
            ''', (market_drop_threshold,))
            return [dict(row) for row in self.cursor.fetchall()]
        finally:
            self.disconnect()

    def get_signal_weights(self) -> Dict[str, float]:
        """Get current signal weighting rules"""
        self.connect()
        try:
            self.cursor.execute('''
            SELECT signal_type, base_weight, description
            FROM signal_rules
            ORDER BY base_weight DESC
            ''')
            return {row['signal_type']: {
                'weight': row['base_weight'],
                'description': row['description']
            } for row in self.cursor.fetchall()}
        finally:
            self.disconnect()

    @staticmethod
    def _defcon_description(level: int) -> str:
        """Get human-readable DEFCON description"""
        descriptions = {
            5: 'DEFCON 5 - PEACETIME: Normal operations',
            4: 'DEFCON 4 - ELEVATED: >2% drop or significant news',
            3: 'DEFCON 3 - CRISIS: >4% drop or signal clustering',
            2: 'DEFCON 2 - PRE-BOTTOM: 3+ tells detected, minute monitoring',
            1: 'DEFCON 1 - EXECUTE: 80%+ confidence, execute trades'
        }
        return descriptions.get(level, 'UNKNOWN')

    def print_full_report(self):
        """Print comprehensive database report"""
        print("\n" + "="*70)
        print("HIGHTRADE DATABASE REPORT")
        print("="*70 + "\n")

        # Statistics
        stats = self.query_crisis_statistics()
        if stats:
            print("📊 CRISIS STATISTICS")
            print(f"  Total Crises Tracked: {stats['total_crises']}")
            print(f"  Avg Market Drop: {stats['avg_drop_percent']}%")
            print(f"  Worst Drop: {stats['worst_drop_percent']}%")
            print(f"  Avg Recovery: +{stats['avg_recovery_percent']}% in {stats['avg_recovery_days']} days\n")

        # Recent DEFCON status
        defcon = self.query_defcon_status()
        if defcon:
            print("🚨 CURRENT DEFCON STATUS")
            print(f"  {defcon['defcon_status']}")
            print(f"  Signal Score: {defcon['signal_score']:.1f}/100")
            if defcon['bond_yield_percent']:
                print(f"  10Y Yield: {defcon['bond_yield_percent']:.2f}%")
            if defcon['vix']:
                print(f"  VIX: {defcon['vix']:.1f}")
            print()

        # Recent crises
        crises = self.query_all_crises()
        if crises:
            print("📈 RECENT CRISES")
            for crisis in crises[:3]:
                print(f"  • {crisis['name']}")
                print(f"    {crisis['category'].title()} | {crisis['start_date']} | Drop: {crisis['market_drop_percent']}%")

        print("\n" + "="*70 + "\n")

if __name__ == '__main__':
    import sys

    query = TradeDataQuery()

    if len(sys.argv) > 1:
        if sys.argv[1] == 'status':
            status = query.query_defcon_status()
            print(json.dumps(status, indent=2))
        elif sys.argv[1] == 'crises':
            crises = query.query_all_crises()
            print(f"Found {len(crises)} crises:")
            for c in crises:
                print(f"  {c['name']} ({c['category']})")
        elif sys.argv[1] == 'stats':
            stats = query.query_crisis_statistics()
            print(json.dumps(stats, indent=2))
        elif sys.argv[1] == 'report':
            query.print_full_report()
    else:
        query.print_full_report()
