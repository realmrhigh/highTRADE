#!/usr/bin/env python3
"""
HighTrade Dashboard - Real-Time System Monitor
Interactive HTML dashboard showing DEFCON status, signals, and monitoring history
"""

import sqlite3
import json
from pathlib import Path
from datetime import datetime, timedelta
import base64

DB_PATH = Path.home() / 'trading_data' / 'trading_history.db'

class Dashboard:
    """Generate interactive HTML dashboard from database"""

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path

    def connect(self):
        self.conn = sqlite3.connect(str(self.db_path))
        self.cursor = self.conn.cursor()
        self.cursor.row_factory = sqlite3.Row

    def disconnect(self):
        self.conn.close()

    def get_current_status(self):
        """Get current DEFCON and signal status"""
        self.connect()
        try:
            self.cursor.execute('''
            SELECT monitoring_date, monitoring_time, bond_10yr_yield, vix_close,
                   defcon_level, signal_score
            FROM signal_monitoring
            ORDER BY monitoring_date DESC, monitoring_time DESC
            LIMIT 1
            ''')
            row = self.cursor.fetchone()
            if row:
                return {
                    'date': row['monitoring_date'],
                    'time': row['monitoring_time'],
                    'bond_yield': row['bond_10yr_yield'],
                    'vix': row['vix_close'],
                    'defcon': row['defcon_level'],
                    'score': row['signal_score']
                }
            return None
        finally:
            self.disconnect()

    def get_monitoring_history(self, days=7):
        """Get monitoring history for chart"""
        self.connect()
        try:
            self.cursor.execute('''
            SELECT monitoring_date, monitoring_time, defcon_level, signal_score,
                   bond_10yr_yield, vix_close
            FROM signal_monitoring
            WHERE monitoring_date >= date('now', ?)
            ORDER BY monitoring_date ASC, monitoring_time ASC
            LIMIT 100
            ''', (f'-{days} days',))
            return [dict(row) for row in self.cursor.fetchall()]
        finally:
            self.disconnect()

    def get_crisis_stats(self):
        """Get statistics on historical crises"""
        self.connect()
        try:
            self.cursor.execute('''
            SELECT
                COUNT(*) as total,
                AVG(market_drop_percent) as avg_drop,
                AVG(recovery_percent) as avg_recovery,
                AVG(recovery_days) as avg_days
            FROM crisis_events
            ''')
            row = self.cursor.fetchone()
            return {
                'total': row['total'],
                'avg_drop': round(row['avg_drop'], 1) if row['avg_drop'] else 0,
                'avg_recovery': round(row['avg_recovery'], 1) if row['avg_recovery'] else 0,
                'avg_days': round(row['avg_days'], 1) if row['avg_days'] else 0
            }
        finally:
            self.disconnect()

    def get_recent_alerts(self, limit=5):
        """Get recent DEFCON escalations"""
        self.connect()
        try:
            self.cursor.execute('''
            SELECT event_date, event_time, defcon_level, reason
            FROM defcon_history
            ORDER BY event_date DESC, event_time DESC
            LIMIT ?
            ''', (limit,))
            return [dict(row) for row in self.cursor.fetchall()]
        finally:
            self.disconnect()

    def get_recent_logs(self, limit=20):
        """Get recent log entries from hightrade log file"""
        try:
            from pathlib import Path
            logs_dir = Path.home() / 'trading_data' / 'logs'

            # Find the latest log file
            if logs_dir.exists():
                log_files = sorted(logs_dir.glob('hightrade_*.log'), reverse=True)
                if log_files:
                    log_file = log_files[0]
                    with open(log_file, 'r') as f:
                        lines = f.readlines()

                    # Get the last N lines, filter out warnings
                    recent_logs = []
                    for line in reversed(lines):
                        if recent_logs and len(recent_logs) >= limit:
                            break
                        # Extract timestamp and message
                        if ' - INFO - ' in line or ' - WARNING - ' in line:
                            try:
                                parts = line.split(' - ', 3)
                                if len(parts) >= 4:
                                    timestamp = parts[0]
                                    level = parts[2]
                                    message = parts[3].strip()
                                    recent_logs.append({
                                        'timestamp': timestamp,
                                        'level': level,
                                        'message': message
                                    })
                            except:
                                pass

                    return list(reversed(recent_logs))
        except Exception as e:
            pass

        return []

    def generate_html(self):
        """Generate complete interactive dashboard HTML"""
        status = self.get_current_status()
        history = self.get_monitoring_history(days=7)
        stats = self.get_crisis_stats()
        alerts = self.get_recent_alerts()
        logs = self.get_recent_logs(limit=15)

        # Import portfolio dashboard for trading metrics
        try:
            from portfolio_dashboard import PortfolioDashboard
            portfolio_db = PortfolioDashboard()
            portfolio_summary = portfolio_db.get_portfolio_summary()
            portfolio_by_asset = portfolio_db.get_performance_by_asset()
            portfolio_allocation = portfolio_db.get_asset_allocation()
            portfolio_open_pos = portfolio_db.get_open_positions_summary()
        except:
            portfolio_summary = None
            portfolio_by_asset = None
            portfolio_allocation = None
            portfolio_open_pos = None

        defcon_colors = {
            5: '#28a745',  # Green
            4: '#ffc107',  # Yellow
            3: '#fd7e14',  # Orange
            2: '#dc3545',  # Red
            1: '#8b0000'   # Dark red
        }

        defcon_names = {
            5: 'PEACETIME',
            4: 'ELEVATED',
            3: 'CRISIS',
            2: 'PRE-BOTTOM',
            1: 'EXECUTE'
        }

        # Prepare chart data
        chart_dates = [h['monitoring_date'] + ' ' + h['monitoring_time'] for h in history]
        chart_defcon = [h['defcon_level'] for h in history]
        chart_score = [h['signal_score'] for h in history]

        html = f'''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>HighTrade Dashboard</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/3.9.1/chart.min.js"></script>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%);
            color: #333;
            padding: 20px;
            min-height: 100vh;
        }}

        .container {{
            max-width: 1400px;
            margin: 0 auto;
        }}

        header {{
            color: white;
            margin-bottom: 30px;
            text-align: center;
        }}

        header h1 {{
            font-size: 2.5em;
            margin-bottom: 10px;
        }}

        .status-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }}

        .card {{
            background: white;
            border-radius: 12px;
            padding: 25px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.2);
        }}

        .defcon-card {{
            background: linear-gradient(135deg, {defcon_colors.get(status['defcon'] if status else 5, '#28a745')} 0%, {defcon_colors.get(status['defcon'] if status else 5, '#28a745')}dd 100%);
            color: white;
            text-align: center;
        }}

        .defcon-level {{
            font-size: 3em;
            font-weight: bold;
            margin: 10px 0;
        }}

        .defcon-name {{
            font-size: 1.5em;
            opacity: 0.95;
        }}

        .defcon-status {{
            font-size: 0.9em;
            margin-top: 10px;
            opacity: 0.85;
        }}

        .metric {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin: 15px 0;
            padding: 10px 0;
            border-bottom: 1px solid #eee;
        }}

        .metric:last-child {{
            border-bottom: none;
        }}

        .metric-label {{
            font-weight: 600;
            color: #666;
        }}

        .metric-value {{
            font-size: 1.3em;
            color: #2a5298;
            font-weight: bold;
        }}

        .chart-container {{
            position: relative;
            height: 400px;
            margin-bottom: 30px;
        }}

        .section-title {{
            font-size: 1.5em;
            font-weight: bold;
            color: white;
            margin: 30px 0 20px 0;
            text-shadow: 0 2px 4px rgba(0,0,0,0.3);
        }}

        .alerts-list {{
            background: white;
            border-radius: 12px;
            padding: 20px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.2);
        }}

        .alert-item {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 15px;
            border-left: 4px solid #2a5298;
            margin-bottom: 10px;
            background: #f8f9fa;
            border-radius: 4px;
        }}

        .alert-item:last-child {{
            margin-bottom: 0;
        }}

        .alert-defcon {{
            font-weight: bold;
            font-size: 1.1em;
        }}

        .alert-time {{
            color: #666;
            font-size: 0.9em;
        }}

        .logs-container {{
            background: white;
            border-radius: 12px;
            padding: 20px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.2);
            margin-bottom: 30px;
            max-height: 400px;
            overflow-y: auto;
            font-family: 'Monaco', 'Courier New', monospace;
            font-size: 0.85em;
        }}

        .log-entry {{
            padding: 10px;
            margin-bottom: 8px;
            border-left: 3px solid #2a5298;
            background: #f8f9fa;
            border-radius: 3px;
            display: flex;
            flex-direction: column;
        }}

        .log-timestamp {{
            color: #666;
            font-weight: 600;
            margin-bottom: 3px;
            font-family: 'Monaco', 'Courier New', monospace;
        }}

        .log-message {{
            color: #333;
            word-break: break-word;
            line-height: 1.4;
        }}

        .log-level-info {{
            border-left-color: #28a745;
        }}

        .log-level-warning {{
            border-left-color: #ffc107;
            background: #fffbea;
        }}

        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-bottom: 30px;
        }}

        .stat-box {{
            background: white;
            border-radius: 8px;
            padding: 20px;
            text-align: center;
            box-shadow: 0 4px 12px rgba(0,0,0,0.1);
        }}

        .stat-value {{
            font-size: 2em;
            font-weight: bold;
            color: #2a5298;
        }}

        .stat-label {{
            color: #666;
            margin-top: 8px;
            font-size: 0.9em;
        }}

        .last-update {{
            text-align: center;
            color: white;
            margin-top: 20px;
            font-size: 0.9em;
            opacity: 0.8;
        }}

        .alert-badge {{
            display: inline-block;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 0.85em;
            font-weight: 600;
        }}

        .badge-defcon5 {{ background: #28a745; color: white; }}
        .badge-defcon4 {{ background: #ffc107; color: black; }}
        .badge-defcon3 {{ background: #fd7e14; color: white; }}
        .badge-defcon2 {{ background: #dc3545; color: white; }}
        .badge-defcon1 {{ background: #8b0000; color: white; }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🚀 HighTrade Dashboard</h1>
            <p>Real-Time Crisis Trading Bot Monitor</p>
        </header>

        <!-- Current Status -->
        <div class="status-grid">
            <div class="card defcon-card">
                <div class="defcon-name">{defcon_names.get(status['defcon'], 'UNKNOWN') if status else 'UNKNOWN'}</div>
                <div class="defcon-level">{status['defcon']}/5</div>
                <div class="defcon-status">
                    Signal Score: {status['score']:.1f}/100<br>
                    {('🟢 Monitoring Passive' if status['defcon'] == 5 else
                      '🟡 Monitoring Active' if status['defcon'] == 4 else
                      '🟠 Crisis Mode' if status['defcon'] == 3 else
                      '🔴 Pre-Bottom' if status['defcon'] == 2 else
                      '🚨 EXECUTE SIGNAL') if status else 'No Data'}
                </div>
            </div>

            <div class="card">
                <div class="metric">
                    <span class="metric-label">10Y Bond Yield</span>
                    <span class="metric-value">{f"{status['bond_yield']:.2f}%" if status and status['bond_yield'] else 'N/A'}</span>
                </div>
                <div class="metric">
                    <span class="metric-label">VIX</span>
                    <span class="metric-value">{f"{status['vix']:.1f}" if status and status['vix'] else 'N/A'}</span>
                </div>
                <div class="metric">
                    <span class="metric-label">Last Update</span>
                    <span class="metric-value" style="font-size: 0.85em;">{f"{status['date']} {status['time']}" if status else 'Never'}</span>
                </div>
            </div>

            <div class="card">
                <h3 style="margin-bottom: 15px; color: #2a5298;">Historical Crisis Stats</h3>
                <div class="metric">
                    <span class="metric-label">Total Crises Tracked</span>
                    <span class="metric-value">{stats['total']}</span>
                </div>
                <div class="metric">
                    <span class="metric-label">Avg Drop</span>
                    <span class="metric-value">{stats['avg_drop']:.1f}%</span>
                </div>
                <div class="metric">
                    <span class="metric-label">Avg Recovery</span>
                    <span class="metric-value">+{stats['avg_recovery']:.1f}%</span>
                </div>
            </div>
        </div>

        <!-- Charts -->
        <div class="section-title">📊 Monitoring History (7 Days)</div>

        <div class="chart-container">
            <canvas id="defconChart"></canvas>
        </div>

        <div class="chart-container">
            <canvas id="scoreChart"></canvas>
        </div>

        <!-- Recent Alerts -->
        <div class="section-title">⚠️ Recent DEFCON Changes</div>

        <div class="alerts-list">
            {self._render_alerts(alerts, defcon_names)}
        </div>

        <!-- Portfolio Trading Section -->
        {self._render_portfolio_section(portfolio_summary, portfolio_by_asset, portfolio_allocation, portfolio_open_pos)}

        <!-- System Logs -->
        <div class="section-title">📋 Recent System Activity</div>

        <div class="logs-container">
            {self._render_logs(logs)}
        </div>

        <div class="last-update">
            Dashboard updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC
        </div>
    </div>

    <script>
        // DEFCON Level Chart
        const defconCtx = document.getElementById('defconChart').getContext('2d');
        new Chart(defconCtx, {{
            type: 'line',
            data: {{
                labels: {json.dumps(chart_dates[-20:])},
                datasets: [{{
                    label: 'DEFCON Level',
                    data: {json.dumps(chart_defcon[-20:])},
                    borderColor: '#2a5298',
                    backgroundColor: 'rgba(42, 82, 152, 0.1)',
                    borderWidth: 2,
                    tension: 0.4,
                    fill: true,
                    pointRadius: 4,
                    pointHoverRadius: 6
                }}]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                plugins: {{
                    legend: {{
                        display: true,
                        labels: {{ font: {{ size: 12 }} }}
                    }},
                    title: {{
                        display: true,
                        text: 'DEFCON Level Over Time (5 = Safe, 1 = Execute)',
                        font: {{ size: 14 }}
                    }}
                }},
                scales: {{
                    y: {{
                        min: 0,
                        max: 5,
                        ticks: {{ stepSize: 1 }}
                    }}
                }}
            }}
        }});

        // Signal Score Chart
        const scoreCtx = document.getElementById('scoreChart').getContext('2d');
        new Chart(scoreCtx, {{
            type: 'line',
            data: {{
                labels: {json.dumps(chart_dates[-20:])},
                datasets: [{{
                    label: 'Signal Score',
                    data: {json.dumps(chart_score[-20:])},
                    borderColor: '#dc3545',
                    backgroundColor: 'rgba(220, 53, 69, 0.1)',
                    borderWidth: 2,
                    tension: 0.4,
                    fill: true,
                    pointRadius: 4,
                    pointHoverRadius: 6
                }}]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                plugins: {{
                    legend: {{
                        display: true,
                        labels: {{ font: {{ size: 12 }} }}
                    }},
                    title: {{
                        display: true,
                        text: 'Signal Confidence Score (80+ = Buy Signal)',
                        font: {{ size: 14 }}
                    }}
                }},
                scales: {{
                    y: {{
                        min: 0,
                        max: 100
                    }}
                }}
            }}
        }});
    </script>
</body>
</html>
'''
        return html

    def _render_portfolio_section(self, portfolio_summary, portfolio_by_asset, portfolio_allocation, portfolio_open_pos):
        """Render portfolio trading section HTML"""
        if not portfolio_summary or portfolio_summary.get('total_trades', 0) == 0:
            return '''
        <div class="section-title">📊 Portfolio Trading (No trades yet)</div>
        <div class="card" style="text-align: center; padding: 30px; color: #666;">
            <p>Start the monitoring system to begin generating paper trades:</p>
            <p><code>python3 hightrade_orchestrator.py continuous</code></p>
        </div>
        '''

        html = '''
        <div class="section-title">📊 Paper Trading Portfolio</div>

        <div class="status-grid">
            <div class="card">
                <h3 style="margin-bottom: 15px; color: #2a5298;">Portfolio Summary</h3>
                <div class="metric">
                    <span class="metric-label">Total Trades</span>
                    <span class="metric-value">{}</span>
                </div>
                <div class="metric">
                    <span class="metric-label">Open Trades</span>
                    <span class="metric-value">{}</span>
                </div>
                <div class="metric">
                    <span class="metric-label">Closed Trades</span>
                    <span class="metric-value">{}</span>
                </div>
                <div class="metric">
                    <span class="metric-label">Total P&L</span>
                    <span class="metric-value" style="color: {};">${}</span>
                </div>
            </div>

            <div class="card">
                <h3 style="margin-bottom: 15px; color: #2a5298;">Performance Metrics</h3>
                <div class="metric">
                    <span class="metric-label">Win Rate</span>
                    <span class="metric-value">{}%</span>
                </div>
                <div class="metric">
                    <span class="metric-label">Winning Trades</span>
                    <span class="metric-value">{}</span>
                </div>
                <div class="metric">
                    <span class="metric-label">Losing Trades</span>
                    <span class="metric-value">{}</span>
                </div>
                <div class="metric">
                    <span class="metric-label">Avg Return</span>
                    <span class="metric-value">{}%</span>
                </div>
            </div>

            <div class="card">
                <h3 style="margin-bottom: 15px; color: #2a5298;">Asset Allocation</h3>
        '''.format(
            portfolio_summary.get('total_trades', 0),
            portfolio_summary.get('open_trades', 0),
            portfolio_summary.get('closed_trades', 0),
            '#28a745' if portfolio_summary.get('total_profit_loss_dollars', 0) >= 0 else '#dc3545',
            f"{portfolio_summary.get('total_profit_loss_dollars', 0):+,.0f}",
            portfolio_summary.get('win_rate', 0),
            portfolio_summary.get('winning_trades', 0),
            portfolio_summary.get('losing_trades', 0),
            portfolio_summary.get('avg_return', 0),
        )

        # Asset allocation
        if portfolio_allocation and portfolio_allocation.get('allocations'):
            for asset, data in sorted(portfolio_allocation['allocations'].items(), key=lambda x: x[1]['total_value'], reverse=True):
                html += f'''
                <div class="metric">
                    <span class="metric-label">{asset}</span>
                    <span class="metric-value">{data['allocation_pct']:.1f}%</span>
                </div>
                '''
        else:
            html += '''
                <div class="metric" style="color: #999;">
                    <span class="metric-label">No open positions</span>
                </div>
            '''

        html += '''
            </div>
        </div>

        <div class="section-title">📈 Performance by Asset</div>
        <div class="card">
            <table style="width: 100%; border-collapse: collapse; font-size: 0.9em;">
                <thead>
                    <tr style="background-color: #f5f5f5; border-bottom: 2px solid #ddd;">
                        <th style="padding: 12px; text-align: left;">Asset</th>
                        <th style="padding: 12px; text-align: center;">Trades</th>
                        <th style="padding: 12px; text-align: right;">P&L</th>
                        <th style="padding: 12px; text-align: center;">Win %</th>
                        <th style="padding: 12px; text-align: right;">Avg Return</th>
                    </tr>
                </thead>
                <tbody>
        '''

        if portfolio_by_asset:
            for asset in sorted(portfolio_by_asset.keys()):
                metrics = portfolio_by_asset[asset]
                pnl_color = '#28a745' if metrics['total_pnl'] >= 0 else '#dc3545'
                html += f'''
                    <tr style="border-bottom: 1px solid #eee;">
                        <td style="padding: 12px;"><strong>{asset}</strong></td>
                        <td style="padding: 12px; text-align: center;">{metrics['total_trades']}</td>
                        <td style="padding: 12px; text-align: right; color: {pnl_color};">${metrics['total_pnl']:+,.0f}</td>
                        <td style="padding: 12px; text-align: center;">{metrics['win_rate']:.0f}%</td>
                        <td style="padding: 12px; text-align: right;">{metrics['avg_return']:+.2f}%</td>
                    </tr>
                '''
        else:
            html += '''
                    <tr>
                        <td colspan="5" style="padding: 12px; text-align: center; color: #999;">No trades yet</td>
                    </tr>
            '''

        html += '''
                </tbody>
            </table>
        </div>

        <div class="section-title">📍 Open Positions</div>
        <div class="card">
        '''

        if portfolio_open_pos:
            html += f'''
            <table style="width: 100%; border-collapse: collapse; font-size: 0.85em;">
                <thead>
                    <tr style="background-color: #f5f5f5; border-bottom: 2px solid #ddd;">
                        <th style="padding: 10px; text-align: left;">Asset</th>
                        <th style="padding: 10px; text-align: center;">Shares</th>
                        <th style="padding: 10px; text-align: right;">Entry Price</th>
                        <th style="padding: 10px; text-align: right;">Value</th>
                        <th style="padding: 10px; text-align: left;">Entry Date</th>
                    </tr>
                </thead>
                <tbody>
            '''
            for pos in portfolio_open_pos:
                html += f'''
                    <tr style="border-bottom: 1px solid #eee;">
                        <td style="padding: 10px;"><strong>{pos['asset_symbol']}</strong></td>
                        <td style="padding: 10px; text-align: center;">{pos['shares']}</td>
                        <td style="padding: 10px; text-align: right;">${pos['entry_price']:.2f}</td>
                        <td style="padding: 10px; text-align: right;">${pos['position_size_dollars']:,.0f}</td>
                        <td style="padding: 10px;">{pos['entry_date']}</td>
                    </tr>
                '''
            html += '''
                </tbody>
            </table>
            '''
        else:
            html += '<p style="text-align: center; color: #999;">No open positions</p>'

        html += '</div>'
        return html

    def _render_alerts(self, alerts, defcon_names):
        """Render recent alerts HTML"""
        if not alerts:
            return '<p style="color: #666; text-align: center;">No DEFCON changes recorded yet</p>'

        html = ''
        badge_classes = {
            5: 'badge-defcon5',
            4: 'badge-defcon4',
            3: 'badge-defcon3',
            2: 'badge-defcon2',
            1: 'badge-defcon1'
        }

        for alert in alerts:
            defcon = alert['defcon_level']
            html += f'''
            <div class="alert-item">
                <div>
                    <span class="alert-badge {badge_classes.get(defcon, 'badge-defcon5')}">
                        DEFCON {defcon}: {defcon_names.get(defcon, 'UNKNOWN')}
                    </span>
                    <div class="alert-time">{alert['event_date']} {alert['event_time']}</div>
                </div>
            </div>
            '''

        return html

    def _render_logs(self, logs):
        """Render recent system logs HTML"""
        if not logs:
            return '<p style="color: #666; text-align: center;">No log entries available yet</p>'

        html = ''
        for log in logs:
            level = log.get('level', 'INFO').strip()
            level_class = f'log-level-{level.lower()}'
            timestamp = log.get('timestamp', '')
            message = log.get('message', '').replace('<', '&lt;').replace('>', '&gt;')

            html += f'''
            <div class="log-entry {level_class}">
                <div class="log-timestamp">{timestamp}</div>
                <div class="log-message">{message}</div>
            </div>
            '''

        return html

    def save_dashboard(self, output_path=None):
        """Generate and save dashboard HTML"""
        if output_path is None:
            output_path = Path.home() / 'trading_data' / 'dashboard.html'

        output_path.parent.mkdir(parents=True, exist_ok=True)

        html_content = self.generate_html()

        with open(output_path, 'w') as f:
            f.write(html_content)

        return output_path

if __name__ == '__main__':
    dashboard = Dashboard()

    # Generate and save dashboard
    output_file = dashboard.save_dashboard()
    print(f"✅ Dashboard generated: {output_file}")
    print(f"\n📊 Open in browser:")
    print(f"   open {output_file}")
    print(f"\n🔄 Auto-refresh every 15 minutes with:")
    print(f"   python3 dashboard.py")
