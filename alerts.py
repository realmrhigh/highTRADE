#!/usr/bin/env python3
"""
HighTrade Alert System - Multi-Channel Notifications
Send SMS, Email, and Slack alerts for DEFCON escalations
"""

import sqlite3
import json
from pathlib import Path
from datetime import datetime
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests
from typing import List, Dict, Any

# Use SCRIPT_DIR to ensure we're in the correct project directory
SCRIPT_DIR = Path(__file__).parent.resolve()
DB_PATH = SCRIPT_DIR / 'trading_data' / 'trading_history.db'
CONFIG_PATH = SCRIPT_DIR / 'trading_data' / 'alert_config.json'

class AlertSystem:
    """Multi-channel alert notification system"""

    def __init__(self, config_path=CONFIG_PATH):
        self.config_path = config_path
        self.config = self.load_config()

    def load_config(self) -> Dict[str, Any]:
        """Load alert configuration"""
        if self.config_path.exists():
            with open(self.config_path, 'r') as f:
                return json.load(f)

        # Default configuration
        default_config = {
            'enabled': True,
            'channels': {
                'email': {
                    'enabled': False,
                    'address': 'stantonhigh@gmail.com',
                    'smtp_server': 'smtp.gmail.com',
                    'smtp_port': 587,
                    'username': '',  # Set this
                    'password': ''   # Set this (or use app password)
                },
                'sms': {
                    'enabled': True,
                    'phone_number': '+1',  # Add your phone number
                    'provider': 'twilio',  # twilio or other
                    'account_sid': '',     # Twilio Account SID
                    'auth_token': ''       # Twilio Auth Token
                },
                'slack': {
                    'enabled': False,
                    'webhook_url': ''
                }
            },
            'alert_thresholds': {
                'defcon_5': False,  # PEACETIME - no alerts
                'defcon_4': False,  # ELEVATED - optional
                'defcon_3': False,  # CRISIS - optional
                'defcon_2': True,   # PRE-BOTTOM - alert
                'defcon_1': True    # EXECUTE - critical alert
            },
            'alert_history': []
        }

        # Save default config
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_path, 'w') as f:
            json.dump(default_config, f, indent=2)

        return default_config

    def save_config(self):
        """Save updated configuration"""
        with open(self.config_path, 'w') as f:
            json.dump(self.config, f, indent=2)

    def should_alert_for_defcon(self, defcon_level: int) -> bool:
        """Check if alert should be sent for this DEFCON level"""
        if not self.config['enabled']:
            return False

        threshold_key = f'defcon_{defcon_level}'
        return self.config['alert_thresholds'].get(threshold_key, False)

    def get_defcon_description(self, level: int) -> str:
        """Get human-readable DEFCON description"""
        descriptions = {
            5: 'PEACETIME - Normal monitoring',
            4: 'ELEVATED - >2% market drop or significant news',
            3: 'CRISIS - >4% drop or signal clustering',
            2: 'PRE-BOTTOM - 3+ tells detected, ready to execute',
            1: 'EXECUTE - 80%+ confidence, BUY SIGNAL'
        }
        return descriptions.get(level, 'UNKNOWN')

    def send_sms(self, message: str, defcon_level: int) -> bool:
        """Send SMS alert via Twilio"""
        if not self.config['channels']['sms']['enabled']:
            return False

        sms_config = self.config['channels']['sms']
        provider = sms_config.get('provider', 'twilio')

        if provider == 'twilio':
            try:
                from twilio.rest import Client

                account_sid = sms_config.get('account_sid')
                auth_token = sms_config.get('auth_token')
                phone_number = sms_config.get('phone_number')

                if not all([account_sid, auth_token, phone_number]):
                    print("⚠️  SMS not configured (missing credentials)")
                    return False

                client = Client(account_sid, auth_token)

                # Twilio sender number (your Twilio number)
                from_number = '+1234567890'  # Replace with your Twilio number

                body = f"🚨 HighTrade Alert\n\nDEFCON {defcon_level}: {self.get_defcon_description(defcon_level)}\n\n{message}"

                message = client.messages.create(
                    body=body,
                    from_=from_number,
                    to=phone_number
                )

                print(f"✅ SMS sent: {message.sid}")
                self._log_alert('sms', defcon_level, True)
                return True

            except ImportError:
                print("⚠️  Twilio library not installed: pip install twilio")
                return False
            except Exception as e:
                print(f"❌ SMS failed: {e}")
                self._log_alert('sms', defcon_level, False, str(e))
                return False
        else:
            print(f"⚠️  SMS provider '{provider}' not implemented")
            return False

    def send_email(self, subject: str, message: str, defcon_level: int) -> bool:
        """Send email alert"""
        if not self.config['channels']['email']['enabled']:
            return False

        email_config = self.config['channels']['email']

        try:
            # Create message
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = email_config['username']
            msg['To'] = email_config['address']

            # Email body
            email_body = f"""
            <html>
                <body>
                    <h2>🚨 HighTrade Alert</h2>
                    <h3>DEFCON {defcon_level}: {self.get_defcon_description(defcon_level)}</h3>
                    <p>{message}</p>
                    <hr>
                    <p>Timestamp: {datetime.now().isoformat()}</p>
                    <p><a href="file://{SCRIPT_DIR / 'trading_data' / 'dashboard.html'}">View Dashboard</a></p>
                </body>
            </html>
            """

            msg.attach(MIMEText(email_body, 'html'))

            # Send email
            with smtplib.SMTP(email_config['smtp_server'], email_config['smtp_port']) as server:
                server.starttls()
                server.login(email_config['username'], email_config['password'])
                server.send_message(msg)

            print(f"✅ Email sent to {email_config['address']}")
            self._log_alert('email', defcon_level, True)
            return True

        except Exception as e:
            print(f"❌ Email failed: {e}")
            self._log_alert('email', defcon_level, False, str(e))
            return False

    def send_slack(self, message: str, defcon_level: int) -> bool:
        """Send Slack message"""
        if not self.config['channels']['slack']['enabled']:
            return False

        slack_config = self.config['channels']['slack']
        webhook_url = slack_config.get('webhook_url')

        if not webhook_url:
            print("⚠️  Slack webhook URL not configured")
            return False

        try:
            # Color based on DEFCON level
            colors = {
                5: '#28a745',  # Green
                4: '#ffc107',  # Yellow
                3: '#fd7e14',  # Orange
                2: '#dc3545',  # Red
                1: '#8b0000'   # Dark red
            }

            payload = {
                'attachments': [
                    {
                        'color': colors.get(defcon_level, '#808080'),
                        'title': f'🚨 HighTrade DEFCON {defcon_level}',
                        'text': f"{self.get_defcon_description(defcon_level)}\n\n{message}",
                        'footer': 'HighTrade Alert System',
                        'ts': int(datetime.now().timestamp())
                    }
                ]
            }

            response = requests.post(webhook_url, json=payload, timeout=5)

            if response.status_code == 200:
                print("✅ Slack message sent")
                self._log_alert('slack', defcon_level, True)
                return True
            else:
                print(f"❌ Slack failed: {response.status_code}")
                self._log_alert('slack', defcon_level, False, f"Status {response.status_code}")
                return False

        except Exception as e:
            print(f"❌ Slack failed: {e}")
            self._log_alert('slack', defcon_level, False, str(e))
            return False

    def send_silent_log(self, event_type: str, data: dict) -> bool:
        """Send silent log message to #logs-silent channel (no notifications)"""
        if 'slack_logging' not in self.config['channels']:
            return False

        logging_config = self.config['channels']['slack_logging']
        if not logging_config.get('enabled', False):
            return False

        webhook_url = logging_config.get('webhook_url')
        if not webhook_url or 'PLACEHOLDER' in webhook_url:
            return False

        # Check if this event type should be logged
        log_events = logging_config.get('log_events', [])
        if event_type not in log_events:
            return False

        try:
            # Format data for logging
            if event_type == 'status':
                text = (
                    f"📊 Status Update\n"
                    f"DEFCON: {data.get('defcon_level', '?')}/5 | "
                    f"Signal: {data.get('signal_score', 0):.1f}/100 | "
                    f"VIX: {data.get('vix', '?')} | "
                    f"Yield: {data.get('bond_yield', '?')}%"
                )
                if 'holdings' in data and data['holdings']:
                    text += f"\nHoldings: {data['holdings']}"

            elif event_type == 'defcon_change':
                text = (
                    f"🚨 DEFCON Changed: {data.get('old_defcon', '?')} → {data.get('new_defcon', '?')}\n"
                    f"Signal Score: {data.get('signal_score', 0):.1f}/100"
                )

            elif event_type == 'wind_down':
                defcon = data.get('defcon', '?')
                cycles = data.get('wind_down_cycles', 0)
                deesc = data.get('deescalation_score', 0)
                text = (
                    f"🔄 WIND-DOWN ACTIVE — DEFCON {defcon}/5\n"
                    f"De-escalating gradually (cycle {cycles})\n"
                    f"De-escalation score: {deesc:.0f}/100\n"
                    f"New position sizing: 50% of normal"
                )

            elif event_type == 'trade_entry':
                text = (
                    f"📈 Trade Entry\n"
                    f"Assets: {data.get('assets', '?')} | "
                    f"Size: ${data.get('position_size', 0):,.0f} | "
                    f"DEFCON: {data.get('defcon', '?')}"
                )

            elif event_type == 'trade_exit':
                text = (
                    f"📉 Trade Exit\n"
                    f"Asset: {data.get('asset', '?')} | "
                    f"Reason: {data.get('reason', '?')} | "
                    f"P&L: {data.get('pnl_pct', 0):+.1f}%"
                )

            elif event_type == 'monitoring_cycle':
                cycle = data.get('cycle', '?')
                defcon = data.get('defcon_level', 5)
                score = data.get('signal_score', 0)
                vix = data.get('vix', '?')
                bond = data.get('bond_yield', '?')

                # Account financials
                account_value = data.get('account_value', 0)
                cash = data.get('cash_available', 0)
                deployed = data.get('deployed', 0)
                realized_pnl = data.get('realized_pnl', 0)
                pnl_pct = data.get('total_pnl_pct', 0)
                win_rate = data.get('win_rate', 0)
                open_trades = data.get('open_trades', 0)
                closed_trades = data.get('closed_trades', 0)

                pnl_emoji = '📈' if realized_pnl >= 0 else '📉'
                pnl_sign = '+' if realized_pnl >= 0 else ''
                defcon_emoji = '🔴' if defcon <= 2 else '🟠' if defcon == 3 else '🟡' if defcon == 4 else '🟢'

                text = (
                    f"🔄 Cycle #{cycle} | {defcon_emoji} DEFCON {defcon} | Score {score:.1f}/100\n"
                    f"📡 VIX: {vix} | 10Y: {bond}%\n"
                    f"💰 Account: ${account_value:,.0f} | Cash: ${cash:,.0f} | Deployed: ${deployed:,.0f}\n"
                    f"{pnl_emoji} Realized P&L: {pnl_sign}${realized_pnl:,.2f} ({pnl_sign}{pnl_pct:.2f}%) | "
                    f"Win Rate: {win_rate:.0f}% | {open_trades} open / {closed_trades} closed"
                )

                # Open positions detail
                positions = data.get('open_positions', [])
                if positions:
                    text += "\n📋 Positions:"
                    for p in positions:
                        sym   = p.get('asset_symbol', '?')
                        shares = p.get('shares', 0)
                        entry  = p.get('entry_price', 0)
                        size   = p.get('position_size_dollars', 0)
                        curr   = p.get('current_price')
                        upnl   = p.get('unrealized_pnl_dollars')
                        upct   = p.get('unrealized_pnl_percent')

                        if curr and upnl is not None:
                            upnl_sign = '+' if upnl >= 0 else ''
                            upnl_emoji = '📈' if upnl >= 0 else '📉'
                            text += (
                                f"\n  • {sym} — {shares} shares | entry ${entry:.2f} → now ${curr:.2f} "
                                f"| {upnl_emoji} {upnl_sign}${upnl:,.2f} ({upnl_sign}{upct:.1f}%)"
                            )
                        else:
                            text += f"\n  • {sym} — ${size:,.0f} ({shares} shares @ ${entry:.2f})"

            elif event_type == 'news_update':
                # Breaking news gets special indicator
                breaking_indicator = "🚨 BREAKING" if data.get('breaking_count', 0) > 0 else "📰"
                sentiment = data.get('sentiment', 'neutral')
                score = data.get('news_score', 0)

                # Score bar visualization (10 blocks)
                filled = int(score / 10)
                score_bar = '█' * filled + '░' * (10 - filled)

                text = (
                    f"{breaking_indicator} News Update\n"
                    f"Score: [{score_bar}] {score:.1f}/100 | Crisis: {data.get('crisis_type', 'N/A')}\n"
                    f"Sentiment: {sentiment} | Articles: {data.get('article_count', 0)}"
                )

                # Score components breakdown
                components = data.get('score_components', {})
                if components:
                    text += (
                        f"\n┌ sentiment={components.get('sentiment_net', 0):.0f} "
                        f"concentration={components.get('signal_concentration', 0):.0f} "
                        f"urgency={components.get('urgency_premium', 0):.0f} "
                        f"specificity={components.get('keyword_specificity', 0):.0f}"
                    )

                # Gemini Flash summary if available
                gemini = data.get('gemini')
                if gemini:
                    action = gemini.get('action', 'WAIT')
                    coherence = gemini.get('coherence', 0)
                    confidence = gemini.get('confidence', 0)
                    theme = gemini.get('theme', '')
                    reasoning = gemini.get('reasoning', '')
                    action_emoji = '🟢' if action == 'BUY' else '🔴' if action == 'SELL' else '🟡' if action == 'HOLD' else '⚪'
                    text += f"\n🤖 Gemini: {action_emoji} {action} | coherence={coherence:.2f} signal_conf={confidence:.2f}"
                    if theme:
                        text += f"\n   Theme: {theme}"
                    if reasoning:
                        text += f"\n   {reasoning[:180]}..."

                # Top 3 headlines
                if 'top_articles' in data and data['top_articles']:
                    text += "\n\nLatest Headlines:"
                    for i, article in enumerate(data['top_articles'][:3], 1):
                        source = article.get('source', 'Unknown')
                        title = article.get('title', 'No title')
                        if len(title) > 80:
                            title = title[:77] + '...'
                        urgency = article.get('urgency', 'routine')
                        urgency_emoji = '🔥' if urgency == 'breaking' else '⚡' if urgency == 'high' else '•'
                        text += f"\n{urgency_emoji} {i}. [{source}] {title}"

            elif event_type == 'congressional_cluster':
                ticker = data.get('ticker', '?')
                count = data.get('buy_count', 0)
                strength = data.get('signal_strength', 0)
                bipartisan = data.get('bipartisan', False)
                committees = data.get('committee_relevance', [])
                politicians = data.get('politicians', [])
                amount = data.get('total_amount', 0)
                window = data.get('window_days', 30)

                bipartisan_flag = " 🤝 BIPARTISAN" if bipartisan else ""
                committee_flag = f" | Committees: {', '.join(committees)}" if committees else ""
                strength_bar = '█' * int(strength / 10) + '░' * (10 - int(strength / 10))

                text = (
                    f"🏛️ Congressional Cluster Buy Signal{bipartisan_flag}\n"
                    f"Ticker: ${ticker} | {count} politicians in {window}-day window\n"
                    f"Signal Strength: [{strength_bar}] {strength:.0f}/100{committee_flag}\n"
                    f"Est. Total: ${amount:,.0f}\n"
                    f"Politicians: {', '.join(politicians[:5])}"
                )

            elif event_type == 'macro_update':
                macro_score = data.get('macro_score', 50)
                defcon_mod = data.get('defcon_modifier', 0)
                bearish = data.get('bearish_count', 0)
                bullish = data.get('bullish_count', 0)
                signals = data.get('signals', [])

                score_bar = '█' * int(macro_score / 10) + '░' * (10 - int(macro_score / 10))
                mod_str = f"{defcon_mod:+.1f}" if defcon_mod != 0 else "±0"

                text = (
                    f"📊 Macro Environment Alert\n"
                    f"Score: [{score_bar}] {macro_score:.0f}/100 | DEFCON modifier: {mod_str}\n"
                    f"Signals: {bearish} bearish, {bullish} bullish"
                )

                # Key indicators
                yc = data.get('yield_curve')
                ff = data.get('fed_funds')
                ur = data.get('unemployment')
                hy = data.get('hy_oas_bps')
                if yc is not None:
                    text += f"\n• Yield Curve: {yc:+.2f}%"
                if ff is not None:
                    text += f" | Fed Funds: {ff:.2f}%"
                if ur is not None:
                    text += f" | Unemployment: {ur:.1f}%"
                if hy is not None:
                    text += f" | HY OAS: {hy:.0f}bps"

                # Top bearish signals
                bearish_sigs = [s for s in signals if s.get('severity') == 'bearish'][:2]
                if bearish_sigs:
                    text += "\n⚠️  Key risks:"
                    for sig in bearish_sigs:
                        text += f"\n  🔴 {sig.get('description', '')}"

            elif event_type == 'position_closed':
                ticker      = data.get('ticker', '?')
                reason      = data.get('reason', 'unknown')
                entry_px    = data.get('entry_price', 0)
                exit_px     = data.get('exit_price', 0)
                pnl_d       = data.get('profit_loss_dollars', 0)
                pnl_pct     = data.get('profit_loss_pct', 0)
                shares      = data.get('shares', 0)
                exit_type   = data.get('decision_type', '')
                holding_hrs = data.get('holding_hours')
                hold_str    = f" | held {holding_hrs:.0f}h" if holding_hrs else ''

                # decision_type overrides plain reason for catalyst exits
                decision_type = data.get('decision_type', '')
                catalyst_event = data.get('catalyst_event', '')

                decision_type_map = {
                    'SELL_TRAILING_STOP':    ('🛑', 'Trailing Stop — -3% From Peak'),
                    'SELL_THESIS_FLOOR':     ('🚨', 'Thesis Floor Breached — Immediate Exit'),
                    'SELL_CATALYST_SPIKE':   ('🚀', 'Catalyst Spike — Sold Into Strength'),
                    'SELL_CATALYST_FAILED':  ('💥', 'Catalyst Failed — Event Went Wrong Direction'),
                    'SELL_CATALYST_EXPIRED': ('⏰', 'Catalyst Expired — No Move Materialized'),
                }
                reason_map = {
                    'stop_loss':     ('🛑', 'Stop Loss'),
                    'profit_target': ('🎯', 'Profit Target'),
                    'manual':        ('🖐', 'Manual Exit'),
                    'invalidation':  ('⚠️', 'Thesis Invalidated'),
                }

                if decision_type in decision_type_map:
                    reason_emoji, reason_label = decision_type_map[decision_type]
                else:
                    reason_emoji, reason_label = reason_map.get(reason, ('💼', reason.replace('_', ' ').title()))

                pnl_emoji = '📈' if pnl_d >= 0 else '📉'
                pnl_sign  = '+' if pnl_d >= 0 else ''
                catalyst_line = f"\n📅 Catalyst: _{catalyst_event}_" if catalyst_event else ''

                text = (
                    f"{reason_emoji} *SELL EXECUTED — {ticker}* · {reason_label}\n"
                    f"Entry `${entry_px:.2f}` → Exit `${exit_px:.2f}`{hold_str}\n"
                    f"{pnl_emoji} P&L: `{pnl_sign}${pnl_d:,.0f}` (`{pnl_sign}{pnl_pct:.2f}%`)"
                    f"{catalyst_line}"
                )

            elif event_type == 'rebound_watchlist':
                ticker      = data.get('ticker', '?')
                loss_pct    = data.get('loss_pct', 0)
                loss_dollars = data.get('loss_dollars', 0)
                exit_price  = data.get('exit_price', 0)
                entry_price = data.get('entry_price', 0)
                text = (
                    f"🔄 *REBOUND WATCHLIST* — `{ticker}` queued for recovery research\n"
                    f"Stop-loss exit: `{loss_pct:.1f}%` | `${abs(loss_dollars):,.0f}` loss\n"
                    f"Exited @ `${exit_price:.2f}` (entered @ `${entry_price:.2f}`)\n"
                    f"Pipeline: researcher → analyst → verifier will find re-entry below `${exit_price:.2f}`"
                )

            elif event_type == 'flash_briefing':
                emoji      = data.get('emoji', '📊')
                label      = data.get('label', '').capitalize()
                summary    = data.get('summary', '')
                defcon     = data.get('defcon', '?')
                macro      = data.get('macro_score', 0)
                in_tok     = data.get('in_tokens', 0)
                out_tok    = data.get('out_tokens', 0)
                gaps       = data.get('gaps', [])
                gaps_line  = f"\n_🔍 Gaps: {', '.join(gaps)}_" if gaps else ""
                text = (
                    f"{emoji} *{label} Flash Briefing* — DEFCON {defcon}/5 | Macro {macro:.0f}/100\n"
                    f"{summary}{gaps_line}\n"
                    f"_({in_tok}→{out_tok} tokens)_"
                )

            elif event_type == 'verifier_alert':
                confirmed   = data.get('confirmed',   0)
                flagged     = data.get('flagged',     0)
                invalidated = data.get('invalidated', 0)
                corrected   = data.get('corrected',   0)
                demoted     = data.get('demoted',     0)
                archived    = data.get('archived',    0)
                defcon      = data.get('defcon',      '?')
                mode        = data.get('mode',        'hourly')
                status_line = f"✅ {confirmed} confirmed"
                if flagged:     status_line += f" · 🚩 {flagged} flagged"
                if corrected:   status_line += f" · 🔄 {corrected} corrected & restored"
                if demoted:     status_line += f" · ⬇️ {demoted} demoted to low-priority"
                if archived:    status_line += f" · 💀 {archived} archived (terminal)"
                if invalidated: status_line += f" · ❌ {invalidated} hard-invalidated"
                any_action = flagged or invalidated or corrected or demoted or archived
                emoji = '⚠️' if any_action else '🔍'
                text = (
                    f"{emoji} *Verifier [{mode}]* — DEFCON {defcon}/5\n"
                    f"{status_line}"
                )

            elif event_type == 'exit_update':
                ticker   = data.get('ticker', '?')
                stop_old = data.get('stop_old')
                stop_new = data.get('stop_new')
                tp1_old  = data.get('tp1_old')
                tp1_new  = data.get('tp1_new')
                tp2_new  = data.get('tp2_new')
                thesis   = (data.get('thesis') or '')[:120]
                def _fmt_lvl(old, new, label):
                    old_str = f"${old:.2f}" if old else "—"
                    new_str = f"*${new:.2f}*" if new else "—"
                    return f"  {label}: {old_str} → {new_str}"
                lines = [f"🔄 *{ticker} exit levels updated* (fresh re-analysis)"]
                if stop_new: lines.append(_fmt_lvl(stop_old, stop_new, "Stop "))
                if tp1_new:  lines.append(_fmt_lvl(tp1_old,  tp1_new,  "TP1  "))
                if tp2_new:  lines.append(f"  TP2 : *${tp2_new:.2f}*")
                if thesis:   lines.append(f"_{thesis}_")
                text = "\n".join(lines)

            elif event_type == 'daytrade_scan':
                ticker = data.get('ticker', '?')
                conf = data.get('confidence', 0)
                catalyst = data.get('catalyst', '')
                thesis = data.get('thesis', '')
                stop = data.get('stop_loss_pct', 0)
                tp = data.get('take_profit_pct', 0)
                stretch = data.get('stretch_target_pct', 0)
                size = data.get('position_size', 0)
                status = data.get('status', 'scanned')
                text = (
                    f"🌅 *Day Trade Scan* — Pick: `{ticker}`\n"
                    f"Confidence: {conf}% | Size: ${size:,.0f} | Status: {status}\n"
                    f"Stop: {stop}% | TP: {tp}% | Stretch: {stretch}%\n"
                    f"Catalyst: {catalyst[:200]}\n"
                    f"_{thesis[:300]}_"
                )

            elif event_type == 'daytrade_result':
                ticker = data.get('ticker', '?')
                pnl_d = data.get('pnl_dollars', 0) or 0
                pnl_pct = data.get('pnl_pct', 0) or 0
                reason = data.get('reason', 'eod')
                entry_px = data.get('entry_price', 0) or 0
                exit_px = data.get('exit_price', 0) or 0
                shares = data.get('shares', 0) or 0
                size = data.get('position_size', 0) or 0
                emoji = '📈' if pnl_d >= 0 else '📉'
                pnl_sign = '+' if pnl_d >= 0 else ''
                text = (
                    f"{emoji} *Day Trade Result* — `{ticker}` ({reason})\n"
                    f"Entry `${entry_px:.2f}` → Exit `${exit_px:.2f}` | {shares} shares\n"
                    f"Size: ${size:,.0f} | P&L: `{pnl_sign}${pnl_d:,.2f}` (`{pnl_sign}{pnl_pct:.2f}%`)"
                )

            else:
                text = f"{event_type}: {json.dumps(data, indent=2)}"

            # Send with no @channel or notification
            payload = {
                'text': text,
                'username': 'HighTrade Logger',
                'icon_emoji': ':robot_face:'
            }

            response = requests.post(webhook_url, json=payload, timeout=5)
            return response.status_code == 200

        except Exception as e:
            # Silent failure for logging - don't disrupt main flow
            return False

    def send_acquisition_alert(self, message: str, primary: bool = False) -> bool:
        """Send acquisition conditional notification.

        primary=False → #logs-silent (pipeline noise, full_auto confirmations).
        primary=True  → #hightrade  (semi_auto triggers that need user /buy action).

        Bypasses the log_events filter in send_silent_log — acquisition alerts
        always go to the target channel regardless of the config list.
        """
        if primary:
            channel_config = self.config.get('channels', {}).get('slack', {})
        else:
            channel_config = self.config.get('channels', {}).get('slack_logging', {})

        if not channel_config.get('enabled', False):
            return False

        webhook_url = channel_config.get('webhook_url')
        if not webhook_url or 'PLACEHOLDER' in webhook_url:
            return False

        try:
            payload = {
                'text': message,
                'username': 'HighTrade Acquisitions',
                'icon_emoji': ':dart:'
            }
            response = requests.post(webhook_url, json=payload, timeout=5)
            return response.status_code == 200
        except Exception:
            return False

    def send_notify(self, event_type: str, data: dict) -> bool:
        """Send a notification to #all-hightrade (primary webhook — triggers push notifications).

        Used for Flash briefings, daily briefings, health reports, and any
        status update the team should be notified about. Formats the same
        event_type payloads as send_silent_log but routes to the main channel.
        """
        slack_config = self.config.get('channels', {}).get('slack', {})
        if not slack_config.get('enabled', False):
            return False

        webhook_url = slack_config.get('webhook_url')
        if not webhook_url or 'PLACEHOLDER' in webhook_url:
            return False

        try:
            # Reuse the same rich formatting from send_silent_log
            # by temporarily routing through it and catching the text output.
            # Instead, we duplicate the format logic here for independence.

            if event_type == 'flash_briefing':
                emoji      = data.get('emoji', '📊')
                label      = data.get('label', '').capitalize()
                summary    = data.get('summary', '')
                defcon     = data.get('defcon', '?')
                macro      = data.get('macro_score', 0)
                in_tok     = data.get('in_tokens', 0)
                out_tok    = data.get('out_tokens', 0)
                gaps       = data.get('gaps', [])
                gaps_line  = f"\n_🔍 Gaps: {', '.join(gaps)}_" if gaps else ""
                text = (
                    f"{emoji} *{label} Flash Briefing* — DEFCON {defcon}/5 | Macro {macro:.0f}/100\n"
                    f"{summary}{gaps_line}\n"
                    f"_({in_tok}→{out_tok} tokens)_"
                )

            elif event_type == 'position_closed':
                ticker        = data.get('ticker', '?')
                reason        = data.get('reason', 'manual')
                entry_px      = data.get('entry_price', 0)
                exit_px       = data.get('exit_price', 0)
                pnl_d         = data.get('profit_loss_dollars', 0)
                pnl_pct       = data.get('profit_loss_pct', 0)
                holding_hrs   = data.get('holding_hours')
                hold_str      = f" | held {holding_hrs:.0f}h" if holding_hrs else ''
                decision_type = data.get('decision_type', '')
                catalyst_event = data.get('catalyst_event', '')

                decision_type_map = {
                    'SELL_TRAILING_STOP':    ('🛑', 'Trailing Stop — -3% From Peak'),
                    'SELL_THESIS_FLOOR':     ('🚨', 'Thesis Floor Breached — Immediate Exit'),
                    'SELL_CATALYST_SPIKE':   ('🚀', 'Catalyst Spike — Sold Into Strength'),
                    'SELL_CATALYST_FAILED':  ('💥', 'Catalyst Failed — Event Went Wrong Direction'),
                    'SELL_CATALYST_EXPIRED': ('⏰', 'Catalyst Expired — No Move Materialized'),
                }
                reason_map = {
                    'stop_loss':     ('🛑', 'Stop Loss'),
                    'profit_target': ('🎯', 'Profit Target'),
                    'manual':        ('🖐', 'Manual Exit'),
                    'invalidation':  ('⚠️', 'Thesis Invalidated'),
                }
                if decision_type in decision_type_map:
                    reason_emoji, reason_label = decision_type_map[decision_type]
                else:
                    reason_emoji, reason_label = reason_map.get(reason, ('💼', reason.replace('_', ' ').title()))

                pnl_emoji = '📈' if pnl_d >= 0 else '📉'
                pnl_sign  = '+' if pnl_d >= 0 else ''
                catalyst_line = f"\n📅 Catalyst: _{catalyst_event}_" if catalyst_event else ''
                text = (
                    f"{reason_emoji} *SELL EXECUTED — {ticker}* · {reason_label}\n"
                    f"Entry `${entry_px:.2f}` → Exit `${exit_px:.2f}`{hold_str}\n"
                    f"{pnl_emoji} P&L: `{pnl_sign}${pnl_d:,.0f}` (`{pnl_sign}{pnl_pct:.2f}%`)"
                    f"{catalyst_line}"
                )

            elif event_type == 'daily_briefing':
                model_key  = data.get('model_key', 'reasoning')
                regime     = data.get('market_regime', 'Unknown')
                headline   = data.get('headline', '')
                biggest_risk = data.get('biggest_risk', '')
                best_opp   = data.get('best_opportunity', '')
                defcon_fc  = data.get('defcon_forecast', '')
                gaps       = data.get('data_gaps', [])
                in_tok     = data.get('in_tokens', 0)
                out_tok    = data.get('out_tokens', 0)
                gaps_line  = f"\n_🔍 Gaps: {', '.join(gaps)}_" if gaps else ""
                text = (
                    f"📋 *Daily Briefing* — {regime}\n"
                    f"{headline}\n"
                    f"⚠️ Risk: {biggest_risk}\n"
                    f"💡 Opportunity: {best_opp}\n"
                    f"🔭 DEFCON Outlook: {defcon_fc}"
                    f"{gaps_line}\n"
                    f"_({in_tok}→{out_tok} tokens)_"
                )

            elif event_type == 'verifier_alert':
                confirmed   = data.get('confirmed',   0)
                flagged     = data.get('flagged',     0)
                invalidated = data.get('invalidated', 0)
                corrected   = data.get('corrected',   0)
                demoted     = data.get('demoted',     0)
                archived    = data.get('archived',    0)
                defcon      = data.get('defcon',      '?')
                mode        = data.get('mode',        'hourly')
                status_line = f"✅ {confirmed} confirmed"
                if flagged:     status_line += f" · 🚩 {flagged} flagged"
                if corrected:   status_line += f" · 🔄 {corrected} corrected & restored"
                if demoted:     status_line += f" · ⬇️ {demoted} demoted to low-priority"
                if archived:    status_line += f" · 💀 {archived} archived (terminal)"
                if invalidated: status_line += f" · ❌ {invalidated} hard-invalidated"
                any_action = flagged or invalidated or corrected or demoted or archived
                emoji = '⚠️' if any_action else '🔍'
                text = (
                    f"{emoji} *Verifier [{mode}]* — DEFCON {defcon}/5\n"
                    f"{status_line}"
                )

            elif event_type == 'health_report':
                status     = data.get('status', 'unknown')   # ok | warning | critical
                summary    = data.get('summary', '')
                new_models = data.get('new_models', [])
                recurring_gaps = data.get('recurring_gaps', [])
                apis_down  = data.get('apis_down', [])
                emoji = {'ok': '✅', 'warning': '⚠️', 'critical': '🚨'}.get(status, '📊')
                sections = [f"{emoji} *Twice-Weekly Health Report* — {status.upper()}", summary]
                if apis_down:
                    sections.append(f"🔴 APIs Down: {', '.join(apis_down)}")
                if new_models:
                    sections.append(f"🆕 Model Updates Available: {', '.join(new_models)}")
                if recurring_gaps:
                    sections.append(f"🔁 Recurring Data Gaps (needs code): {', '.join(recurring_gaps)}")
                text = '\n'.join(sections)

            elif event_type == 'hound_alert':
                ticker = data.get('ticker', '?')
                score  = data.get('score', 0)
                thesis = data.get('thesis', '')
                risks  = data.get('risks', [])
                action = data.get('action', 'monitor')
                
                score_bar = '🚀' * int(score / 20) + '⚪' * (5 - int(score / 20))
                text = (
                    f"🐕 *Grok Hound Alert* — `${ticker}`\n"
                    f"Alpha Score: {score_bar} ({score}/100)\n"
                    f"🎯 *Thesis:* {thesis}\n"
                    f"⚠️ *Risks:* {', '.join(risks[:3])}\n"
                    f"🛠️ *Suggestion:* {action.upper()}"
                )

            elif event_type == 'daytrade_result':
                ticker = data.get('ticker', '?')
                pnl_d = data.get('pnl_dollars', 0) or 0
                pnl_pct = data.get('pnl_pct', 0) or 0
                reason = data.get('reason', 'eod')
                entry_px = data.get('entry_price', 0) or 0
                exit_px = data.get('exit_price', 0) or 0
                shares = data.get('shares', 0) or 0
                size = data.get('position_size', 0) or 0
                conf = data.get('confidence', 0) or 0
                emoji = '📈' if pnl_d >= 0 else '📉'
                pnl_sign = '+' if pnl_d >= 0 else ''
                reason_map = {
                    'stop_loss': '🛑 Stop Loss',
                    'profit_target': '🎯 Take Profit',
                    'eod': '⏰ EOD Exit',
                    'manual': '🖐 Manual',
                }
                reason_label = reason_map.get(reason, reason)
                text = (
                    f"{emoji} *Day Trade Result* — `{ticker}` · {reason_label}\n"
                    f"Entry `${entry_px:.2f}` → Exit `${exit_px:.2f}` | {shares} shares\n"
                    f"Size: ${size:,.0f} (conf {conf}%) | P&L: `{pnl_sign}${pnl_d:,.2f}` (`{pnl_sign}{pnl_pct:.2f}%`)"
                )

            elif event_type == 'uw_flow_sweep':
                ticker    = data.get('ticker', '?')
                premium   = data.get('premium', 0)
                sentiment = data.get('sentiment', 'unknown').upper()
                digest    = data.get('digest', '')
                count     = data.get('count', 1)
                emoji     = '🟢' if sentiment == 'BULLISH' else '🔴' if sentiment == 'BEARISH' else '🟡'
                text = (
                    f"{emoji} *UW Big Sweep{'s' if count > 1 else ''}: {count} ticker{'s' if count > 1 else ''}*\n"
                    f"{digest if digest else f'{ticker} {sentiment} ${premium:,.0f}'}\n"
                    f"_Unusual Whales — size > OI, >${1}M+ net premium_"
                )

            else:
                text = f"📢 *{event_type}*: {json.dumps(data, indent=2)}"

            # Post via bot token + chat.postMessage so Slack treats it as
            # a proper bot message — this is what triggers user notifications.
            # Webhook posts arrive silently; bot posts respect channel notification
            # settings and show up in the activity feed.
            bot_token  = slack_config.get('bot_token', '')
            channel_id = slack_config.get('channel_id', '')

            # Route daytrade_result to #all-highpay if configured
            if event_type == 'daytrade_result':
                daytrade_channel = self.config.get('channels', {}).get('daytrade', {}).get('channel_id', '')
                if daytrade_channel:
                    channel_id = daytrade_channel

            if bot_token and channel_id:
                response = requests.post(
                    'https://slack.com/api/chat.postMessage',
                    headers={
                        'Authorization': f'Bearer {bot_token}',
                        'Content-Type': 'application/json',
                    },
                    json={
                        'channel':  channel_id,
                        'text':     text,
                        'username': 'HighTrade',
                        'icon_emoji': ':chart_with_upwards_trend:',
                    },
                    timeout=5,
                )
                data = response.json()
                return data.get('ok', False)
            else:
                # Fallback to webhook if bot token not configured
                response = requests.post(webhook_url, json={'text': text}, timeout=5)
                return response.status_code == 200

        except Exception:
            return False

    def send_defcon_alert(self, defcon_level: int, signal_score: float, details: str = ""):
        """Send comprehensive alert for DEFCON escalation"""
        if not self.should_alert_for_defcon(defcon_level):
            return

        # Suppress non-critical alerts outside market hours (Mon-Fri 8:00-17:00 ET)
        # DEFCON 1 always fires regardless of time
        if defcon_level > 1:
            try:
                from datetime import datetime as _dt
                from zoneinfo import ZoneInfo as _ZI
                _now = _dt.now(_ZI('America/New_York'))
                _in_window = _now.weekday() < 5 and 8 <= _now.hour < 17
                if not _in_window:
                    return
            except Exception:
                pass

        print(f"\n📢 Sending alerts for DEFCON {defcon_level}...")

        # Craft message
        message = f"""
Signal Score: {signal_score:.1f}/100
Status: {self.get_defcon_description(defcon_level)}
Time: {datetime.now().isoformat()}

{details}

View full dashboard for details.
        """.strip()

        subject = f"HighTrade Alert: DEFCON {defcon_level} - {self.get_defcon_description(defcon_level)}"

        # Send via enabled channels
        results = {}

        if self.config['channels']['sms']['enabled']:
            results['sms'] = self.send_sms(message, defcon_level)

        if self.config['channels']['email']['enabled']:
            results['email'] = self.send_email(subject, message, defcon_level)

        if self.config['channels']['slack']['enabled']:
            results['slack'] = self.send_slack(message, defcon_level)

        return results

    def _log_alert(self, channel: str, defcon_level: int, success: bool, error: str = ""):
        """Log alert to history"""
        log_entry = {
            'timestamp': datetime.now().isoformat(),
            'channel': channel,
            'defcon_level': defcon_level,
            'success': success,
            'error': error
        }

        self.config['alert_history'].append(log_entry)

        # Keep only last 100 alerts
        if len(self.config['alert_history']) > 100:
            self.config['alert_history'] = self.config['alert_history'][-100:]

        self.save_config()

    def get_alert_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent alert history"""
        return self.config['alert_history'][-limit:]

    def print_config(self):
        """Print current configuration"""
        print("\n" + "="*70)
        print("ALERT SYSTEM CONFIGURATION")
        print("="*70)
        print(f"System Enabled: {self.config['enabled']}")
        print("\nChannels:")
        for channel, settings in self.config['channels'].items():
            print(f"  {channel.upper()}:")
            print(f"    Enabled: {settings.get('enabled', False)}")
            if channel == 'sms':
                print(f"    Phone: {settings.get('phone_number', 'NOT SET')}")
            elif channel == 'email':
                print(f"    Address: {settings.get('address', 'NOT SET')}")
            elif channel == 'slack':
                print(f"    Webhook: {'SET' if settings.get('webhook_url') else 'NOT SET'}")

        print("\nAlert Thresholds:")
        for level, enabled in self.config['alert_thresholds'].items():
            level_num = int(level.split('_')[1])
            status = "✓ ALERT" if enabled else "✗ NO ALERT"
            print(f"  DEFCON {level_num}: {status}")

        print("\nRecent Alerts:")
        if self.config['alert_history']:
            for alert in self.config['alert_history'][-3:]:
                status = "✓" if alert['success'] else "✗"
                print(f"  {status} {alert['channel'].upper()} @ {alert['timestamp']}")
        else:
            print("  (none)")

        print("="*70 + "\n")

if __name__ == '__main__':
    import sys

    alerts = AlertSystem()

    if len(sys.argv) > 1 and sys.argv[1] == 'config':
        alerts.print_config()
    elif len(sys.argv) > 1 and sys.argv[1] == 'test':
        # Test alert
        print("📤 Sending test alert...")
        alerts.send_defcon_alert(
            defcon_level=2,
            signal_score=65.5,
            details="This is a test alert to verify notification system."
        )
    else:
        alerts.print_config()
        print("Usage:")
        print("  python3 alerts.py config     - Show configuration")
        print("  python3 alerts.py test       - Send test alert")
