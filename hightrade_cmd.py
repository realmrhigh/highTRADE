#!/usr/bin/env python3
"""
HighTrade Slash Command Interface
Send commands to the running orchestrator via a shared command file.

Usage:
  python3 hightrade_cmd.py /status
  python3 hightrade_cmd.py /yes
  python3 hightrade_cmd.py /estop
  python3 hightrade_cmd.py              (interactive mode)
"""

import json
import sys
import os
import time
import logging
from pathlib import Path
from datetime import datetime

SCRIPT_DIR = Path(__file__).parent.resolve()
CMD_DIR = SCRIPT_DIR / 'trading_data' / 'commands'
CMD_FILE = CMD_DIR / 'pending_command.json'
RESPONSE_FILE = CMD_DIR / 'command_response.json'
LOG_FILE = CMD_DIR / 'command_history.json'

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Available Commands
# ─────────────────────────────────────────────────────────────

COMMANDS = {
    '/yes': {
        'description': 'Approve pending trade or action',
        'aliases': ['/y', '/approve'],
        'category': 'decisions',
    },
    '/no': {
        'description': 'Reject pending trade or action',
        'aliases': ['/n', '/reject', '/deny'],
        'category': 'decisions',
    },
    '/hold': {
        'description': 'Pause trading — keep monitoring but do not execute',
        'aliases': ['/pause', '/wait'],
        'category': 'control',
    },
    '/start': {
        'description': 'Resume trading after a hold',
        'aliases': ['/resume', '/go'],
        'category': 'control',
    },
    '/stop': {
        'description': 'Gracefully stop the bot after the current cycle',
        'aliases': ['/quit', '/shutdown'],
        'category': 'control',
    },
    '/estop': {
        'description': 'Emergency stop — halt ALL activity immediately',
        'aliases': ['/emergency', '/kill', '/panic'],
        'category': 'control',
    },
    '/update': {
        'description': 'Force an immediate monitoring cycle (skip wait)',
        'aliases': ['/refresh', '/cycle', '/now'],
        'category': 'control',
    },
    '/status': {
        'description': 'Show current system status and DEFCON level',
        'aliases': ['/info', '/s'],
        'category': 'info',
    },
    '/portfolio': {
        'description': 'Show portfolio summary and open positions',
        'aliases': ['/pf', '/positions'],
        'category': 'info',
    },
    '/defcon': {
        'description': 'Show current DEFCON level and signal scores',
        'aliases': ['/dc', '/alert'],
        'category': 'info',
    },
    '/trades': {
        'description': 'Show pending and recent trades',
        'aliases': ['/pending', '/recent'],
        'category': 'info',
    },
    '/broker': {
        'description': 'Show broker agent status and decision history',
        'aliases': ['/agent'],
        'category': 'info',
    },
    '/mode': {
        'description': 'Switch broker mode (disabled/semi_auto/full_auto). Usage: /mode semi_auto',
        'aliases': [],
        'category': 'config',
    },
    '/interval': {
        'description': 'Change monitoring interval. Usage: /interval 5',
        'aliases': ['/freq'],
        'category': 'config',
    },
    '/buy': {
        'description': 'Manually open a paper position. Usage: /buy TICKER SHARES [@ PRICE]',
        'aliases': ['/long'],
        'category': 'decisions',
    },
    '/sell': {
        'description': 'Manually close a paper position. Usage: /sell TICKER [TRADE_ID]',
        'aliases': ['/exit', '/close'],
        'category': 'decisions',
    },
    '/briefing': {
        'description': 'Run daily market briefing now (Gemini 3 Pro, deep reasoning)',
        'aliases': ['/daily', '/report'],
        'category': 'info',
    },
    '/research': {
        'description': 'Run acquisition researcher now for pending queue',
        'aliases': ['/scan', '/fetch'],
        'category': 'control',
    },
    '/hunt': {
        'description': 'Run Grok Hound now — scan X for high-alpha momentum candidates',
        'aliases': ['/hound', '/sniff'],
        'category': 'control',
    },
    '/help': {
        'description': 'Show all available commands',
        'aliases': ['/h', '/?'],
        'category': 'info',
    },
}

# Build reverse lookup for aliases
ALIAS_MAP = {}
for cmd, meta in COMMANDS.items():
    ALIAS_MAP[cmd] = cmd
    for alias in meta['aliases']:
        ALIAS_MAP[alias] = cmd


# ─────────────────────────────────────────────────────────────
# Command Sender (client side)
# ─────────────────────────────────────────────────────────────

def send_command(raw_input: str) -> dict:
    """Write a command to the shared command file for the orchestrator to pick up."""
    CMD_DIR.mkdir(parents=True, exist_ok=True)

    parts = raw_input.strip().split(None, 1)
    cmd_name = parts[0].lower() if parts else ''
    cmd_args = parts[1] if len(parts) > 1 else ''

    # Resolve alias
    canonical = ALIAS_MAP.get(cmd_name)
    if not canonical:
        return {'ok': False, 'error': f"Unknown command: {cmd_name}. Type /help for available commands."}

    # Handle /help locally
    if canonical == '/help':
        print_help()
        return {'ok': True, 'handled_locally': True}

    command_payload = {
        'command': canonical,
        'args': cmd_args,
        'timestamp': datetime.now().isoformat(),
        'raw': raw_input.strip(),
    }

    # Clear any stale response before writing command so _wait_for_response
    # never picks up a previous command's result
    RESPONSE_FILE.unlink(missing_ok=True)

    # Write command atomically
    tmp_file = CMD_FILE.with_suffix('.tmp')
    with open(tmp_file, 'w') as f:
        json.dump(command_payload, f, indent=2)
    tmp_file.rename(CMD_FILE)

    # Append to history log
    _log_command(command_payload)

    print(f"📤  Sent: {canonical}" + (f" {cmd_args}" if cmd_args else ""))

    # Wait for response (up to 30s for most, 2s for info commands)
    timeout = 5 if canonical in ['/status', '/portfolio', '/defcon', '/trades', '/broker', '/help'] else 30
    response = _wait_for_response(timeout)

    if response:
        _print_response(response)
        return response
    else:
        print("⏳  Command sent. Bot will process it on next wake-up.")
        return {'ok': True, 'pending': True}


def _wait_for_response(timeout: int):
    """Poll for response from the orchestrator."""
    start = time.time()
    while time.time() - start < timeout:
        if RESPONSE_FILE.exists():
            try:
                with open(RESPONSE_FILE, 'r') as f:
                    response = json.load(f)
                RESPONSE_FILE.unlink(missing_ok=True)
                return response
            except (json.JSONDecodeError, IOError):
                pass
        time.sleep(0.3)
    return None


def _print_response(response: dict):
    """Pretty-print a command response."""
    status_icon = "✅" if response.get('ok') else "❌"
    print(f"\n{status_icon}  {response.get('message', 'Done')}")

    if response.get('data'):
        data = response['data']
        if isinstance(data, str):
            print(data)
        elif isinstance(data, dict):
            for key, val in data.items():
                print(f"  {key}: {val}")
        elif isinstance(data, list):
            for item in data:
                print(f"  • {item}")

    if response.get('warning'):
        print(f"⚠️   {response['warning']}")


def _log_command(payload: dict):
    """Append command to history log."""
    history = []
    if LOG_FILE.exists():
        try:
            with open(LOG_FILE, 'r') as f:
                history = json.load(f)
        except (json.JSONDecodeError, IOError):
            history = []

    history.append(payload)
    # Keep last 200
    history = history[-200:]

    with open(LOG_FILE, 'w') as f:
        json.dump(history, f, indent=2)


def print_help():
    """Print all available slash commands."""
    print("\n" + "=" * 65)
    print("  HighTrade Slash Commands")
    print("=" * 65)

    categories = {
        'decisions': '🎯 Decisions',
        'control':   '🎛️  Control',
        'info':      '📊 Information',
        'config':    '⚙️  Configuration',
    }

    for cat_key, cat_label in categories.items():
        cmds = [(c, m) for c, m in COMMANDS.items() if m['category'] == cat_key]
        if not cmds:
            continue
        print(f"\n  {cat_label}")
        print("  " + "-" * 50)
        for cmd, meta in cmds:
            aliases = ', '.join(meta['aliases']) if meta['aliases'] else ''
            alias_str = f"  ({aliases})" if aliases else ''
            print(f"    {cmd:<14} {meta['description']}{alias_str}")

    print("\n" + "=" * 65)
    print("  Usage:  python3 hightrade_cmd.py /status")
    print("          python3 hightrade_cmd.py       (interactive mode)")
    print("=" * 65 + "\n")


# ─────────────────────────────────────────────────────────────
# Command Processor (orchestrator side — imported by orchestrator)
# ─────────────────────────────────────────────────────────────

class CommandProcessor:
    """Processes slash commands inside the running orchestrator."""

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self.trading_hold = False
        self.stop_requested = False
        self.estop_triggered = False
        CMD_DIR.mkdir(parents=True, exist_ok=True)

    @property
    def should_stop(self) -> bool:
        return self.stop_requested or self.estop_triggered

    @property
    def should_skip_trades(self) -> bool:
        return self.trading_hold or self.estop_triggered

    def check_for_commands(self) -> list:
        """Check for and process any pending commands. Returns list of processed commands."""
        processed = []

        if not CMD_FILE.exists():
            return processed

        try:
            with open(CMD_FILE, 'r') as f:
                payload = json.load(f)
            CMD_FILE.unlink(missing_ok=True)
        except (json.JSONDecodeError, IOError):
            return processed

        cmd = payload.get('command', '')
        args = payload.get('args', '')
        logger.info(f"📥 Command received: {cmd}" + (f" {args}" if args else ""))

        response = self._dispatch(cmd, args)
        processed.append({'command': cmd, 'args': args, 'response': response})

        # Write response for the waiting client
        self._send_response(response)

        return processed

    def _dispatch(self, cmd: str, args: str) -> dict:
        """Route command to handler."""
        handlers = {
            '/yes':       self._handle_yes,
            '/no':        self._handle_no,
            '/hold':      self._handle_hold,
            '/start':     self._handle_start,
            '/stop':      self._handle_stop,
            '/estop':     self._handle_estop,
            '/update':    self._handle_update,
            '/status':    self._handle_status,
            '/portfolio': self._handle_portfolio,
            '/defcon':    self._handle_defcon,
            '/trades':    self._handle_trades,
            '/broker':    self._handle_broker,
            '/mode':      self._handle_mode,
            '/interval':  self._handle_interval,
            '/buy':       self._handle_buy,
            '/sell':      self._handle_sell,
            '/briefing':  self._handle_briefing,
            '/research':  self._handle_research,
            '/hunt':      self._handle_hunt,
        }

        handler = handlers.get(cmd)
        if handler:
            try:
                return handler(args)
            except Exception as e:
                logger.error(f"Command handler error: {e}", exc_info=True)
                return {'ok': False, 'message': f"Error processing {cmd}: {e}"}
        else:
            return {'ok': False, 'message': f"Unknown command: {cmd}"}

    # ── Decision Commands ─────────────────────────────────────

    def _handle_research(self, args: str) -> dict:
        """Force-run the acquisition research cycle."""
        logger.info("🔬 /research — forcing acquisition researcher run now...")
        try:
            from acquisition_researcher import run_research_cycle
            tickers = run_research_cycle()
            return {
                'ok': True,
                'message': f'Research cycle complete for: {", ".join(tickers) if tickers else "None pending"}'
            }
        except Exception as e:
            return {'ok': False, 'message': f'Research failed: {e}'}

    def _handle_hunt(self, args: str) -> dict:
        """Force-run the Grok Hound now — scan X for high-alpha candidates."""
        logger.info("🐕 /hunt — forcing Grok Hound run now...")
        try:
            from acquisition_hound import GrokHound
            from fred_macro import FREDMacroTracker
            from pathlib import Path
            DB_PATH = Path(__file__).parent / 'trading_data' / 'trading_history.db'

            # Get current system state for context
            orch = self.orchestrator
            state = {
                'defcon_level': getattr(orch, '_last_defcon', 3),
                'macro_score':  getattr(orch, '_last_macro_score', 50),
            }

            hound = GrokHound(db_path=str(DB_PATH))
            result = hound.hunt(state)
            candidates = result.get('candidates', [])
            promoted = hound.save_candidates(result)

            names = [c.get('ticker', '?') for c in candidates]
            mood  = result.get('hound_mood', 'neutral')
            msg   = (
                f"🐕 Hound ({mood}): found {len(candidates)} candidates"
                + (f" → {', '.join(names)}" if names else '')
                + (f" | auto-promoted: {', '.join(promoted)}" if promoted else '')
            )
            return {'ok': True, 'message': msg}
        except Exception as e:
            return {'ok': False, 'message': f'Hunt failed: {e}'}

    def _handle_yes(self, args: str) -> dict:
        orch = self.orchestrator
        pending_trades = len(orch.pending_trade_alerts)
        pending_exits = len(orch.pending_trade_exits)

        if pending_trades == 0 and pending_exits == 0:
            return {'ok': True, 'message': 'No pending actions to approve.'}

        executed = []
        if pending_trades:
            trade_ids = orch.execute_pending_trades(auto_approve=True)
            executed.append(f"{len(trade_ids)} trade(s) executed")
        if pending_exits:
            exit_ids = orch.execute_pending_exits(auto_exit=True)
            executed.append(f"{len(exit_ids)} position(s) exited")

        summary = ', '.join(executed)
        logger.info(f"✅ /yes — Approved: {summary}")
        return {'ok': True, 'message': f"Approved: {summary}"}

    def _handle_no(self, args: str) -> dict:
        orch = self.orchestrator
        cleared_trades = len(orch.pending_trade_alerts)
        cleared_exits = len(orch.pending_trade_exits)
        orch.pending_trade_alerts.clear()
        orch.pending_trade_exits.clear()

        msg = f"Rejected {cleared_trades} pending trade(s) and {cleared_exits} pending exit(s)."
        logger.info(f"❌ /no — {msg}")
        return {'ok': True, 'message': msg}

    # ── Control Commands ──────────────────────────────────────

    def _handle_hold(self, args: str) -> dict:
        self.trading_hold = True
        logger.warning("⏸️  TRADING HOLD — monitoring continues, no trades will execute")
        # Notify via Slack
        self.orchestrator.alerts.send_slack(
            "⏸️ TRADING HOLD activated by user. Monitoring continues, trades paused.",
            defcon_level=self.orchestrator.previous_defcon
        )
        return {'ok': True, 'message': 'Trading HOLD activated. Monitoring continues, trades paused.'}

    def _handle_start(self, args: str) -> dict:
        self.trading_hold = False
        logger.info("▶️  TRADING RESUMED — trades will execute normally")
        self.orchestrator.alerts.send_slack(
            "▶️ TRADING RESUMED by user. Bot is fully operational.",
            defcon_level=self.orchestrator.previous_defcon
        )
        return {'ok': True, 'message': 'Trading RESUMED. Bot fully operational.'}

    def _handle_stop(self, args: str) -> dict:
        self.stop_requested = True
        logger.warning("🛑 STOP requested — bot will shut down after current cycle")
        self.orchestrator.alerts.send_slack(
            "🛑 GRACEFUL STOP requested. Bot will shut down after current cycle.",
            defcon_level=self.orchestrator.previous_defcon
        )
        return {'ok': True, 'message': 'Graceful stop requested. Will shut down after current cycle.'}

    def _handle_estop(self, args: str) -> dict:
        self.estop_triggered = True
        self.trading_hold = True
        self.stop_requested = True

        # Clear ALL pending actions
        self.orchestrator.pending_trade_alerts.clear()
        self.orchestrator.pending_trade_exits.clear()

        logger.critical("🚨🚨🚨 EMERGENCY STOP — ALL ACTIVITY HALTED 🚨🚨🚨")
        self.orchestrator.alerts.send_slack(
            "🚨 EMERGENCY STOP triggered by user! ALL trading halted. All pending actions cleared.",
            defcon_level=1
        )
        return {
            'ok': True,
            'message': '🚨 EMERGENCY STOP — All activity halted. Pending actions cleared. Bot shutting down.',
        }

    def _handle_update(self, args: str) -> dict:
        logger.info("🔄 Forced update — running monitoring cycle now")
        self.orchestrator.run_monitoring_cycle()
        return {'ok': True, 'message': 'Monitoring cycle completed.'}

    # ── Info Commands ─────────────────────────────────────────

    def _handle_status(self, args: str) -> dict:
        orch = self.orchestrator
        status = orch.monitor.get_status() or {}

        data = {
            'DEFCON Level':     f"{status.get('defcon_level', '?')}/5",
            'Signal Score':     f"{status.get('signal_score', 0):.1f}/100",
            'Broker Mode':      orch.broker_mode.upper(),
            'Trading Hold':     '⏸️  YES' if self.trading_hold else '▶️  No',
            'Cycles Run':       orch.monitoring_cycles,
            'Alerts Sent':      orch.alerts_sent,
            'Pending Trades':   len(orch.pending_trade_alerts),
            'Pending Exits':    len(orch.pending_trade_exits),
            'Bond Yield':       f"{status.get('bond_yield', '?')}%",
            'VIX':              status.get('vix', '?'),
        }

        # Add tracked holdings with current prices
        holdings_info = self._get_holdings_prices()
        if holdings_info:
            data['Holdings'] = holdings_info

        return {'ok': True, 'message': 'System Status', 'data': data}

    def _get_holdings_prices(self) -> str:
        """Get current prices for all open positions (holdings)."""
        open_positions = self.orchestrator.paper_trading.get_open_positions()

        if not open_positions:
            return None

        prices_list = []
        for pos in open_positions:
            symbol = pos['asset_symbol']
            entry_price = pos['entry_price']

            # Get current price
            try:
                current_price = self.orchestrator.paper_trading._get_current_price(symbol)
                if current_price and current_price > 0:
                    pnl_pct = ((current_price - entry_price) / entry_price) * 100
                    arrow = '📈' if pnl_pct >= 0 else '📉'
                    prices_list.append(
                        f"{symbol}: ${current_price:.2f} ({pnl_pct:+.1f}%) {arrow}"
                    )
                else:
                    prices_list.append(f"{symbol}: Price unavailable")
            except Exception:
                prices_list.append(f"{symbol}: ${entry_price:.2f} (entry)")

        return '\n    '.join([''] + prices_list) if prices_list else None

    def _handle_portfolio(self, args: str) -> dict:
        perf = orch_perf = self.orchestrator.paper_trading.get_portfolio_performance()
        open_pos = self.orchestrator.paper_trading.get_open_positions()

        positions_list = []
        for pos in open_pos:
            positions_list.append(
                f"{pos['asset_symbol']}: {pos['shares']} shares @ ${pos['entry_price']:.2f} "
                f"(${pos['position_size_dollars']:,.0f})"
            )

        data = {
            'Total Trades':   perf['total_trades'],
            'Open':           perf['open_trades'],
            'Closed':         perf['closed_trades'],
            'Win Rate':       f"{perf['win_rate']:.1f}%" if perf['closed_trades'] > 0 else 'N/A',
            'Total P&L':      f"${perf['total_profit_loss_dollars']:+,.0f}" if perf['closed_trades'] > 0 else 'N/A',
        }

        if positions_list:
            data['Open Positions'] = '\n    '.join([''] + positions_list)

        return {'ok': True, 'message': 'Portfolio Summary', 'data': data}

    def _handle_defcon(self, args: str) -> dict:
        status = self.orchestrator.monitor.get_status() or {}
        defcon = status.get('defcon_level', 5)

        defcon_labels = {
            5: '🟢 PEACETIME', 4: '🟡 ELEVATED', 3: '🟠 CRISIS',
            2: '🔴 PRE-BOTTOM', 1: '🔴🔴 EXECUTE'
        }

        data = {
            'DEFCON':        f"{defcon}/5 — {defcon_labels.get(defcon, '?')}",
            'Signal Score':  f"{status.get('signal_score', 0):.1f}/100",
            'Bond Yield':    f"{status.get('bond_yield', '?')}%",
            'VIX':           status.get('vix', '?'),
            'Last Check':    f"{status.get('date', '?')} {status.get('time', '?')}",
        }
        return {'ok': True, 'message': 'DEFCON Status', 'data': data}

    def _handle_trades(self, args: str) -> dict:
        orch = self.orchestrator
        pending = orch.pending_trade_alerts
        exits = orch.pending_trade_exits

        info = []
        if pending:
            for i, alert in enumerate(pending, 1):
                info.append(
                    f"📋 Pending #{i}: {alert['assets']['primary_asset']} / "
                    f"{alert['assets']['secondary_asset']} / {alert['assets']['tertiary_asset']} "
                    f"— ${alert['total_position_size']:,.0f}"
                )
        else:
            info.append("No pending trade alerts")

        if exits:
            for ex in exits:
                info.append(
                    f"🚪 Exit: {ex['asset_symbol']} — {ex['reason']} ({ex['profit_loss_pct']:+.2f}%)"
                )

        return {'ok': True, 'message': 'Trade Queue', 'data': info}

    def _handle_broker(self, args: str) -> dict:
        broker_status = self.orchestrator.broker.get_status()
        data = {
            'Auto Execute':     '✅ Yes' if broker_status['auto_execute'] else '❌ No',
            'Trades Today':     f"{broker_status['trades_today']}/{broker_status['daily_limit']}",
            'Can Trade':        '✅ Yes' if broker_status['can_trade'] else '❌ Limit reached',
            'Decision History': f"{broker_status['decision_history_size']} decisions",
        }
        return {'ok': True, 'message': 'Broker Agent Status', 'data': data}

    # ── Config Commands ───────────────────────────────────────

    def _handle_mode(self, args: str) -> dict:
        valid_modes = ['disabled', 'semi_auto', 'full_auto']
        new_mode = args.strip().lower()

        if new_mode not in valid_modes:
            return {
                'ok': False,
                'message': f"Invalid mode. Choose from: {', '.join(valid_modes)}",
            }

        old_mode = self.orchestrator.broker_mode
        self.orchestrator.broker_mode = new_mode

        # Update broker auto_execute
        auto = new_mode in ['semi_auto', 'full_auto']
        self.orchestrator.broker.auto_execute = auto

        logger.info(f"🔧 Broker mode changed: {old_mode} → {new_mode}")
        self.orchestrator.alerts.send_slack(
            f"🔧 Broker mode changed: {old_mode.upper()} → {new_mode.upper()}",
            defcon_level=self.orchestrator.previous_defcon
        )
        return {'ok': True, 'message': f'Broker mode: {old_mode} → {new_mode}'}

    def _handle_interval(self, args: str) -> dict:
        try:
            new_interval = int(args.strip())
            if new_interval < 1 or new_interval > 120:
                return {'ok': False, 'message': 'Interval must be 1–120 minutes.'}

            # Store for the orchestrator to pick up
            self.orchestrator._new_interval = new_interval
            logger.info(f"🔧 Monitoring interval → {new_interval} minutes (takes effect next cycle)")
            return {'ok': True, 'message': f'Interval will change to {new_interval} minutes next cycle.'}
        except ValueError:
            return {'ok': False, 'message': 'Usage: /interval <minutes>  (e.g. /interval 5)'}

    def _handle_buy(self, args: str) -> dict:
        """
        /buy TICKER SHARES [@PRICE]
        Examples:
          /buy MSOS 100
          /buy AAPL 50 @ 195.00
          /buy NVDA 10
        """
        # Parse: "TICKER SHARES" or "TICKER SHARES @ PRICE"
        parts = args.upper().replace('@', '').split()
        if len(parts) < 2:
            return {'ok': False, 'message': 'Usage: /buy TICKER SHARES  (e.g. /buy MSOS 100)'}

        ticker = parts[0]
        try:
            shares = int(parts[1])
        except ValueError:
            return {'ok': False, 'message': f'Invalid share count: {parts[1]}'}

        price_override = None
        if len(parts) >= 3:
            try:
                price_override = float(parts[2])
            except ValueError:
                pass

        result = self.orchestrator.paper_trading.manual_buy(
            ticker, shares, price_override=price_override
        )

        if result['ok']:
            # Send Slack notification
            self.orchestrator.alerts.send_silent_log('trade_entry', {
                'asset': ticker,
                'shares': shares,
                'price': result['entry_price'],
                'size': result['position_size'],
                'trade_id': result['trade_id'],
                'reason': 'Manual buy via /buy command'
            })
            logger.info(f"🛒 /buy — {result['message']}")

        return {'ok': result['ok'], 'message': result['message']}

    def _handle_sell(self, args: str) -> dict:
        """
        /sell TICKER [TRADE_ID] [@PRICE]
        Examples:
          /sell MSOS
          /sell MSOS 7
          /sell MSFT @ 390.00
        """
        parts = args.upper().replace('@', '').split()
        if not parts:
            return {'ok': False, 'message': 'Usage: /sell TICKER [TRADE_ID]  (e.g. /sell MSOS)'}

        ticker = parts[0]
        trade_id = None
        price_override = None

        for p in parts[1:]:
            try:
                val = float(p)
                if val == int(val) and val < 100000 and trade_id is None:
                    trade_id = int(val)   # Looks like a trade_id
                else:
                    price_override = val  # Looks like a price
            except ValueError:
                pass

        result = self.orchestrator.paper_trading.manual_sell(
            ticker, trade_id=trade_id, price_override=price_override
        )

        if result['ok']:
            # Send Slack notification
            self.orchestrator.alerts.send_silent_log('trade_exit', {
                'asset': ticker,
                'reason': 'Manual sell via /sell command',
                'pnl_pct': result.get('pnl_pct', 0),
                'pnl_dollars': result.get('pnl_dollars', 0)
            })
            logger.info(f"💰 /sell — {result['message']}")

        return {'ok': result['ok'], 'message': result['message']}

    def _handle_briefing(self, args: str) -> dict:
        """Force-run the daily market briefing immediately."""
        logger.info("📋 /briefing — forcing daily briefing run now...")
        try:
            self.orchestrator._check_daily_briefing(force=True)
            return {
                'ok': True,
                'message': 'Daily briefing complete — results posted to #logs-silent and saved to DB.'
            }
        except Exception as e:
            return {'ok': False, 'message': f'Briefing failed: {e}'}

    # ── Response Writer ───────────────────────────────────────

    def _send_response(self, response: dict):
        """Write response for the command client."""
        try:
            tmp = RESPONSE_FILE.with_suffix('.tmp')
            with open(tmp, 'w') as f:
                json.dump(response, f, indent=2)
            tmp.rename(RESPONSE_FILE)
        except Exception as e:
            logger.error(f"Failed to write command response: {e}")


# ─────────────────────────────────────────────────────────────
# Main — Interactive / Single-command mode
# ─────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) > 1:
        # Single command from CLI args
        raw = ' '.join(sys.argv[1:])
        if not raw.startswith('/'):
            raw = '/' + raw
        send_command(raw)
    else:
        # Interactive mode
        print_help()
        print("Type a command (or /help). Ctrl-C to exit.\n")
        try:
            while True:
                raw = input("hightrade> ").strip()
                if not raw:
                    continue
                if not raw.startswith('/'):
                    raw = '/' + raw
                if raw in ['/exit', '/quit', '/q']:
                    print("Goodbye!")
                    break
                send_command(raw)
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye!")


if __name__ == '__main__':
    main()
