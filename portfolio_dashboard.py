#!/usr/bin/env python3
"""
HighTrade Portfolio Dashboard - Trading Performance Visualizations
Generates dashboard sections for portfolio metrics and trading performance
"""

import sqlite3
import json
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Any

DB_PATH = Path.home() / 'trading_data' / 'trading_history.db'


class PortfolioDashboard:
    """Generate portfolio performance dashboard visualizations"""

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path

    def connect(self):
        self.conn = sqlite3.connect(str(self.db_path))
        self.cursor = self.conn.cursor()
        self.cursor.row_factory = sqlite3.Row

    def disconnect(self):
        self.conn.close()

    def get_portfolio_summary(self) -> Dict[str, Any]:
        """Get overall portfolio metrics"""
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
                AVG(CASE WHEN status = 'closed' THEN profit_loss_percent ELSE NULL END) as avg_return,
                SUM(CASE WHEN status = 'open' THEN position_size_dollars ELSE 0 END) as open_value
            FROM trade_records
            ''')
            row = self.cursor.fetchone()
            if row:
                row = dict(row)
                win_rate = 0
                if row['closed_trades'] and row['closed_trades'] > 0:
                    win_rate = (row['winning_trades'] / row['closed_trades']) * 100

                return {
                    'total_trades': row['total_trades'] or 0,
                    'open_trades': row['open_trades'] or 0,
                    'closed_trades': row['closed_trades'] or 0,
                    'total_pnl': row['total_pnl'] or 0.0,
                    'winning_trades': row['winning_trades'] or 0,
                    'losing_trades': row['losing_trades'] or 0,
                    'win_rate': round(win_rate, 1),
                    'avg_return': round(row['avg_return'] or 0, 2),
                    'open_value': row['open_value'] or 0.0
                }
            return {}
        finally:
            self.disconnect()

    def get_open_positions_summary(self) -> List[Dict[str, Any]]:
        """Get summary of all open positions"""
        self.connect()
        try:
            self.cursor.execute('''
            SELECT
                trade_id,
                asset_symbol,
                entry_date,
                entry_price,
                shares,
                position_size_dollars,
                defcon_at_entry
            FROM trade_records
            WHERE status = 'open'
            ORDER BY entry_date DESC
            ''')
            return [dict(row) for row in self.cursor.fetchall()]
        finally:
            self.disconnect()

    def get_performance_by_asset(self) -> Dict[str, Dict[str, Any]]:
        """Get performance metrics grouped by asset"""
        self.connect()
        try:
            self.cursor.execute('''
            SELECT
                asset_symbol,
                COUNT(*) as total_trades,
                SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) as closed_trades,
                SUM(CASE WHEN status = 'closed' AND profit_loss_dollars > 0 THEN 1 ELSE 0 END) as winners,
                SUM(CASE WHEN status = 'closed' THEN profit_loss_dollars ELSE 0 END) as total_pnl,
                AVG(CASE WHEN status = 'closed' THEN profit_loss_percent ELSE NULL END) as avg_return
            FROM trade_records
            GROUP BY asset_symbol
            ORDER BY total_pnl DESC
            ''')

            result = {}
            for row in self.cursor.fetchall():
                row = dict(row)
                win_rate = 0
                if row['closed_trades'] and row['closed_trades'] > 0:
                    win_rate = (row['winners'] / row['closed_trades']) * 100

                result[row['asset_symbol']] = {
                    'total_trades': row['total_trades'],
                    'closed_trades': row['closed_trades'] or 0,
                    'winners': row['winners'] or 0,
                    'total_pnl': round(row['total_pnl'] or 0, 2),
                    'avg_return': round(row['avg_return'] or 0, 2),
                    'win_rate': round(win_rate, 1)
                }

            return result
        finally:
            self.disconnect()

    def get_performance_by_crisis_type(self) -> Dict[str, Dict[str, Any]]:
        """Get performance metrics grouped by crisis type"""
        self.connect()
        try:
            self.cursor.execute('''
            SELECT
                COALESCE(c.category, 'signal') as crisis_type,
                COUNT(t.trade_id) as total_trades,
                SUM(CASE WHEN t.status = 'closed' THEN 1 ELSE 0 END) as closed_trades,
                SUM(CASE WHEN t.status = 'closed' AND t.profit_loss_dollars > 0 THEN 1 ELSE 0 END) as winners,
                SUM(CASE WHEN t.status = 'closed' THEN t.profit_loss_dollars ELSE 0 END) as total_pnl,
                AVG(CASE WHEN t.status = 'closed' THEN t.profit_loss_percent ELSE NULL END) as avg_return
            FROM trade_records t
            LEFT JOIN crisis_events c ON t.crisis_id = c.crisis_id
            GROUP BY COALESCE(c.category, 'signal')
            ORDER BY total_pnl DESC
            ''')

            result = {}
            for row in self.cursor.fetchall():
                row = dict(row)
                win_rate = 0
                if row['closed_trades'] and row['closed_trades'] > 0:
                    win_rate = (row['winners'] / row['closed_trades']) * 100

                result[row['crisis_type']] = {
                    'total_trades': row['total_trades'],
                    'closed_trades': row['closed_trades'] or 0,
                    'winners': row['winners'] or 0,
                    'total_pnl': round(row['total_pnl'] or 0, 2),
                    'avg_return': round(row['avg_return'] or 0, 2),
                    'win_rate': round(win_rate, 1)
                }

            return result
        finally:
            self.disconnect()

    def get_recent_trades(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent closed trades with results"""
        self.connect()
        try:
            self.cursor.execute('''
            SELECT
                trade_id,
                asset_symbol,
                entry_date,
                entry_price,
                exit_date,
                exit_price,
                profit_loss_dollars,
                profit_loss_percent,
                exit_reason,
                holding_hours
            FROM trade_records
            WHERE status = 'closed'
            ORDER BY exit_date DESC, exit_date DESC
            LIMIT ?
            ''', (limit,))
            return [dict(row) for row in self.cursor.fetchall()]
        finally:
            self.disconnect()

    def get_asset_allocation(self) -> Dict[str, Any]:
        """Get current portfolio asset allocation (open positions)"""
        self.connect()
        try:
            self.cursor.execute('''
            SELECT
                asset_symbol,
                COUNT(*) as position_count,
                SUM(position_size_dollars) as total_value
            FROM trade_records
            WHERE status = 'open'
            GROUP BY asset_symbol
            ORDER BY total_value DESC
            ''')

            allocations = {}
            total_value = 0

            rows = [dict(row) for row in self.cursor.fetchall()]
            for row in rows:
                total_value += row['total_value'] or 0

            for row in rows:
                pct = 0
                if total_value > 0:
                    pct = (row['total_value'] / total_value) * 100

                allocations[row['asset_symbol']] = {
                    'position_count': row['position_count'],
                    'total_value': round(row['total_value'], 2),
                    'allocation_pct': round(pct, 1)
                }

            return {
                'allocations': allocations,
                'total_value': round(total_value, 2)
            }
        finally:
            self.disconnect()

    def generate_portfolio_html_section(self) -> str:
        """Generate HTML section for portfolio dashboard"""
        summary = self.get_portfolio_summary()
        by_asset = self.get_performance_by_asset()
        by_crisis = self.get_performance_by_crisis_type()
        allocation = self.get_asset_allocation()
        open_positions = self.get_open_positions_summary()
        recent_trades = self.get_recent_trades(limit=5)

        asset_labels = list(by_asset.keys())
        asset_pnls = [by_asset[a]['total_pnl'] for a in asset_labels]
        asset_wins = [by_asset[a]['win_rate'] for a in asset_labels]

        crisis_labels = list(by_crisis.keys())
        crisis_pnls = [by_crisis[c]['total_pnl'] for c in crisis_labels]

        alloc_labels = list(allocation['allocations'].keys())
        alloc_values = [allocation['allocations'][a]['total_value'] for a in alloc_labels]

        # Format recent trades HTML
        recent_trades_html = ""
        if recent_trades:
            for trade in recent_trades:
                pnl_class = "positive" if trade['profit_loss_dollars'] and trade['profit_loss_dollars'] > 0 else "negative"
                recent_trades_html += f"""
                <tr>
                    <td>{trade['asset_symbol']}</td>
                    <td>${trade['entry_price']:.2f}</td>
                    <td>${trade['exit_price']:.2f}</td>
                    <td class="{pnl_class}">${trade['profit_loss_dollars']:+,.0f} ({trade['profit_loss_percent']:+.2f}%)</td>
                    <td>{trade['exit_reason']}</td>
                </tr>
                """
        else:
            recent_trades_html = "<tr><td colspan='5'>No closed trades yet</td></tr>"

        # Format open positions HTML
        open_pos_html = ""
        if open_positions:
            for pos in open_positions:
                open_pos_html += f"""
                <tr>
                    <td>{pos['asset_symbol']}</td>
                    <td>{pos['shares']}</td>
                    <td>${pos['entry_price']:.2f}</td>
                    <td>${pos['position_size_dollars']:,.0f}</td>
                    <td>{pos['entry_date']}</td>
                </tr>
                """
        else:
            open_pos_html = "<tr><td colspan='5'>No open positions</td></tr>"

        html = f"""
        <section class="portfolio-section">
            <h2>📊 Portfolio Performance</h2>

            <div class="portfolio-grid">
                <div class="card">
                    <h3>Portfolio Summary</h3>
                    <div class="metric">
                        <span class="label">Total Trades:</span>
                        <span class="value">{summary.get('total_trades', 0)}</span>
                    </div>
                    <div class="metric">
                        <span class="label">Open Trades:</span>
                        <span class="value">{summary.get('open_trades', 0)}</span>
                    </div>
                    <div class="metric">
                        <span class="label">Closed Trades:</span>
                        <span class="value">{summary.get('closed_trades', 0)}</span>
                    </div>
                    <div class="metric">
                        <span class="label">Total P&L:</span>
                        <span class="value" style="color: {'green' if summary.get('total_pnl', 0) > 0 else 'red'}">
                            ${summary.get('total_pnl', 0):+,.0f}
                        </span>
                    </div>
                    <div class="metric">
                        <span class="label">Win Rate:</span>
                        <span class="value">{summary.get('win_rate', 0):.1f}%</span>
                    </div>
                    <div class="metric">
                        <span class="label">Avg Return:</span>
                        <span class="value">{summary.get('avg_return', 0):+.2f}%</span>
                    </div>
                </div>

                <div class="card">
                    <h3>Performance by Asset</h3>
                    <table class="performance-table">
                        <thead>
                            <tr>
                                <th>Asset</th>
                                <th>Trades</th>
                                <th>P&L</th>
                                <th>Win %</th>
                            </tr>
                        </thead>
                        <tbody>
        """

        for asset in sorted(by_asset.keys()):
            metrics = by_asset[asset]
            pnl_color = 'green' if metrics['total_pnl'] > 0 else 'red'
            html += f"""
                            <tr>
                                <td><strong>{asset}</strong></td>
                                <td>{metrics['total_trades']}</td>
                                <td style="color: {pnl_color}">${metrics['total_pnl']:+,.0f}</td>
                                <td>{metrics['win_rate']:.0f}%</td>
                            </tr>
            """

        html += """
                        </tbody>
                    </table>
                </div>
            </div>

            <div class="portfolio-grid">
                <div class="card">
                    <h3>Asset Allocation (Open Positions)</h3>
                    <canvas id="allocationChart"></canvas>
                </div>

                <div class="card">
                    <h3>P&L by Asset</h3>
                    <canvas id="assetPnlChart"></canvas>
                </div>

                <div class="card">
                    <h3>P&L by Crisis Type</h3>
                    <canvas id="crisisPnlChart"></canvas>
                </div>
            </div>

            <div class="card">
                <h3>Open Positions</h3>
                <table class="trades-table">
                    <thead>
                        <tr>
                            <th>Asset</th>
                            <th>Shares</th>
                            <th>Entry Price</th>
                            <th>Position Value</th>
                            <th>Entry Date</th>
                        </tr>
                    </thead>
                    <tbody>
                        """ + open_pos_html + """
                    </tbody>
                </table>
            </div>

            <div class="card">
                <h3>Recent Closed Trades</h3>
                <table class="trades-table">
                    <thead>
                        <tr>
                            <th>Asset</th>
                            <th>Entry Price</th>
                            <th>Exit Price</th>
                            <th>P&L</th>
                            <th>Exit Reason</th>
                        </tr>
                    </thead>
                    <tbody>
                        """ + recent_trades_html + """
                    </tbody>
                </table>
            </div>

            <script>
                // Allocation Pie Chart
                const allocationCtx = document.getElementById('allocationChart').getContext('2d');
                new Chart(allocationCtx, {{
                    type: 'pie',
                    data: {{
                        labels: {json.dumps(alloc_labels)},
                        datasets: [{{
                            data: {json.dumps(alloc_values)},
                            backgroundColor: [
                                '#FF6384', '#36A2EB', '#FFCE56', '#4BC0C0',
                                '#9966FF', '#FF9F40', '#FF6384', '#C9CBCF'
                            ]
                        }}]
                    }},
                    options: {{
                        responsive: true,
                        plugins: {{
                            legend: {{ position: 'bottom' }}
                        }}
                    }}
                }});

                // Asset P&L Bar Chart
                const assetPnlCtx = document.getElementById('assetPnlChart').getContext('2d');
                new Chart(assetPnlCtx, {{
                    type: 'bar',
                    data: {{
                        labels: {json.dumps(asset_labels)},
                        datasets: [{{
                            label: 'Total P&L ($)',
                            data: {json.dumps(asset_pnls)},
                            backgroundColor: {json.dumps([('rgba(75, 192, 75, 0.7)' if x > 0 else 'rgba(255, 99, 99, 0.7)') for x in asset_pnls])}
                        }}]
                    }},
                    options: {{
                        responsive: true,
                        scales: {{
                            y: {{ beginAtZero: true }}
                        }}
                    }}
                }});

                // Crisis Type P&L Bar Chart
                const crisisPnlCtx = document.getElementById('crisisPnlChart').getContext('2d');
                new Chart(crisisPnlCtx, {{
                    type: 'bar',
                    data: {{
                        labels: {json.dumps(crisis_labels)},
                        datasets: [{{
                            label: 'Total P&L ($)',
                            data: {json.dumps(crisis_pnls)},
                            backgroundColor: {json.dumps([('rgba(75, 192, 75, 0.7)' if x > 0 else 'rgba(255, 99, 99, 0.7)') for x in crisis_pnls])}
                        }}]
                    }},
                    options: {{
                        responsive: true,
                        scales: {{
                            y: {{ beginAtZero: true }}
                        }}
                    }}
                }});
            </script>

            <style>
                .portfolio-section {{
                    margin-top: 40px;
                }}

                .portfolio-grid {{
                    display: grid;
                    grid-template-columns: repeat(auto-fit, minmax(350px, 1fr));
                    gap: 20px;
                    margin-bottom: 30px;
                }}

                .performance-table, .trades-table {{
                    width: 100%;
                    border-collapse: collapse;
                    font-size: 0.9em;
                }}

                .performance-table th, .trades-table th {{
                    background-color: #f5f5f5;
                    padding: 10px;
                    text-align: left;
                    border-bottom: 2px solid #ddd;
                }}

                .performance-table td, .trades-table td {{
                    padding: 10px;
                    border-bottom: 1px solid #eee;
                }}

                .performance-table tr:hover, .trades-table tr:hover {{
                    background-color: #f9f9f9;
                }}

                .metric {{
                    display: flex;
                    justify-content: space-between;
                    padding: 10px 0;
                    border-bottom: 1px solid #eee;
                }}

                .metric .label {{
                    font-weight: 500;
                }}

                .metric .value {{
                    font-weight: bold;
                    color: #2c3e50;
                }}

                .positive {{ color: #28a745 !important; }}
                .negative {{ color: #dc3545 !important; }}
            </style>
        </section>
        """

        return html


def main():
    """Test portfolio dashboard"""
    dashboard = PortfolioDashboard()

    summary = dashboard.get_portfolio_summary()
    by_asset = dashboard.get_performance_by_asset()
    by_crisis = dashboard.get_performance_by_crisis_type()

    print("\n" + "="*70)
    print("PORTFOLIO DASHBOARD TEST")
    print("="*70)
    print("\nPortfolio Summary:")
    print(json.dumps(summary, indent=2))
    print("\nPerformance by Asset:")
    print(json.dumps(by_asset, indent=2))
    print("\nPerformance by Crisis Type:")
    print(json.dumps(by_crisis, indent=2))
    print("="*70 + "\n")


if __name__ == '__main__':
    main()
