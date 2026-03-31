#!/usr/bin/env python3
"""
HighTrade Orchestrator - Main System Controller
Coordinates database builder, news watcher, and notification system
Runs autonomously to gather data, analyze it, and send alerts
"""

import sys
import os
import logging
import json
import atexit
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import time
import errno

try:
    import fcntl
except ImportError:
    fcntl = None

# Load .env early - before any module that reads os.getenv (e.g. AlpacaBroker)
# override=True ensures .env always wins over stale shell/launchd env vars
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(Path(__file__).parent / ".env", override=True)
except ImportError:
    pass  # python-dotenv not installed; rely on shell environment

# All market-schedule decisions use Eastern Time regardless of server location
_ET = ZoneInfo('America/New_York')

def _et_now() -> datetime:
    """Current datetime in US/Eastern - used for all trading-schedule comparisons."""
    return datetime.now(_ET)
from monitoring import SignalMonitor
from alerts import AlertSystem
from dashboard import generate_dashboard_html
from crisis_db_utils import CrisisDatabase
from paper_trading import PaperTradingEngine
from broker_agent import AutonomousBroker, UPSIDE_TRIGGER_TAGS
from hightrade_cmd import CommandProcessor
from news_aggregator import NewsAggregator
from news_sentiment import NewsSentimentAnalyzer
from news_signals import NewsSignalGenerator
from config_validator import ConfigValidator
import exit_analyst

# Configuration
SCRIPT_DIR = Path(__file__).parent.resolve()
DB_PATH = SCRIPT_DIR / 'trading_data' / 'trading_history.db'
LOGS_PATH = SCRIPT_DIR / 'logs'               # unified log dir - matches launchd stdout
LEGACY_LOGS_PATH = SCRIPT_DIR / 'trading_data' / 'logs'  # keep for other components
CONFIG_PATH = SCRIPT_DIR / 'trading_data' / 'orchestrator_config.json'
ORCHESTRATOR_LOCK_PATH = SCRIPT_DIR / 'trading_data' / 'hightrade_orchestrator.lock'

# Create logs directories
LOGS_PATH.mkdir(parents=True, exist_ok=True)
LEGACY_LOGS_PATH.mkdir(parents=True, exist_ok=True)

# Set up logging - force=True overrides any root logger already configured by
# imported modules; StreamHandler goes to stdout which launchd writes to
# logs/orchestrator.log; FileHandler writes a dated backup copy.
LOG_FILE = LOGS_PATH / f"hightrade_{datetime.now().strftime('%Y%m%d')}.log"
_file_handler = logging.FileHandler(LOG_FILE)
_file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(logging.Formatter('%(levelname)s:%(name)s:%(message)s'))
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[_file_handler, _stream_handler],
    force=True,
)
# Ensure log lines are flushed immediately so tail -f works in real time
sys.stdout.reconfigure(line_buffering=True)
logger = logging.getLogger(__name__)


class SingleInstanceLock:
    """Best-effort OS lock to ensure only one orchestrator process is active."""

    def __init__(self, lock_path: Path):
        self.lock_path = Path(lock_path)
        self._fh = None
        self.acquired = False

    def acquire(self) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.lock_path, 'a+')

        if fcntl is None:
            # On platforms without fcntl, keep running without a hard lock.
            # macOS has fcntl, so this is just a defensive fallback.
            self._write_metadata()
            self.acquired = True
            return True

        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise

        self._write_metadata()
        self.acquired = True
        return True

    def _write_metadata(self):
        if not self._fh:
            return
        self._fh.seek(0)
        self._fh.truncate()
        payload = {
            'pid': os.getpid(),
            'started_at': datetime.now().isoformat(),
            'argv': sys.argv,
        }
        self._fh.write(json.dumps(payload))
        self._fh.flush()

    def read_holder(self) -> str:
        try:
            return self.lock_path.read_text().strip()
        except Exception:
            return ''

    def release(self):
        if not self._fh:
            return
        try:
            if fcntl is not None and self.acquired:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass
        self._fh = None
        self.acquired = False


_INSTANCE_LOCK = SingleInstanceLock(ORCHESTRATOR_LOCK_PATH)


def ensure_single_orchestrator_instance() -> None:
    """Exit early if another orchestrator instance already owns the runtime lock."""
    if _INSTANCE_LOCK.acquire():
        atexit.register(_INSTANCE_LOCK.release)
        return

    holder = _INSTANCE_LOCK.read_holder()
    holder_msg = f" Lock holder: {holder}" if holder else ""
    logger.error(
        "❌ Another HighTrade orchestrator instance is already running. "
        f"Exiting to avoid duplicate schedulers/verifier runs.{holder_msg}"
    )
    raise SystemExit(1)


# Module-level handle to allow other modules (broker) to register pending alerts
ORCH_INSTANCE = None

class HighTradeOrchestrator:
    """Main orchestrator for HighTrade system"""
    
    def __init__(self, broker_mode='semi_auto', broker_mode_explicit: bool = False):
        """Initialize orchestrator components

        broker_mode options:
          - 'disabled': Paper trading only, user approval required
          - 'semi_auto': Alerts sent, trades executed with tips
          - 'full_auto': Complete autonomous trading
        """
        # Allow persisted mode to override the default, but never override
        # an explicit CLI/startup selection.
        try:
            if not broker_mode_explicit and CONFIG_PATH.exists():
                with open(CONFIG_PATH) as _f:
                    _cfg = json.load(_f)
                saved_mode = _cfg.get('broker_mode')
                if saved_mode in ('disabled', 'semi_auto', 'full_auto'):
                    broker_mode = saved_mode
        except Exception:
            pass

        logger.info("Initializing HighTrade Orchestrator...")

        # Run startup health checks
        logger.info("🔍 Running startup health checks...")
        validator = ConfigValidator()
        if not validator.validate_all():
            logger.error("❌ Configuration validation failed - check errors above")
            logger.warning("⚠️  Continuing anyway, but system may not function correctly")
        logger.info("")

        self.monitor = SignalMonitor(DB_PATH)
        self.alerts = AlertSystem()
        self.paper_trading = PaperTradingEngine(DB_PATH, total_capital=1000)

        # Initialize broker agent
        # semi_auto: executes signal-driven trades but requires Slack approval for acquisitions
        # full_auto: executes everything autonomously
        auto_execute = broker_mode in ['semi_auto', 'full_auto']
        self.broker = AutonomousBroker(auto_execute=auto_execute, max_daily_trades=5,
                                       broker_mode=broker_mode)
        self.broker_mode = broker_mode

        # NEW: Initialize news digger components
        try:
            logger.info("📰 Initializing News Digger Bot...")
            self.news_aggregator = NewsAggregator('news_config.json')
            self.news_sentiment = NewsSentimentAnalyzer()
            self.news_signal_gen = NewsSignalGenerator()
            self.news_enabled = True
            logger.info("✅ News Digger Bot initialized successfully")
        except Exception as e:
            logger.warning(f"⚠️  News Digger initialization failed: {e}")
            logger.warning("   Continuing with quantitative signals only")
            self.news_enabled = False

        # Initialize new data gap modules
        try:
            from sector_rotation import SectorRotationAnalyzer
            from vix_term_structure import VIXTermStructure
            self.sector_analyzer = SectorRotationAnalyzer()
            self.vix_analyzer = VIXTermStructure()
            logger.info("📡 Data gap modules initialized (Sector Rotation + VIX Term Structure)")
        except Exception as e:
            logger.warning(f"⚠️  Data gap modules init failed: {e}")
            self.sector_analyzer = None
            self.vix_analyzer = None

        # Initialize AI analyzers
        try:
            from gemini_analyzer import GeminiAnalyzer, GrokAnalyzer
            from acquisition_hound import GrokHound
            self.gemini = GeminiAnalyzer()
            self.grok = GrokAnalyzer()
            self.hound = GrokHound(db_path=str(DB_PATH))
            self.gemini_enabled = True
            self.grok_enabled = True
            self.hound_enabled = True
            self._hound_scan_cycle = 0
            self._hound_scan_interval = 4   # Every ~60 min (4 × 15-min cycles)
            logger.info("🤖 AI Analyzers initialized (Gemini Flash/Pro + Grok + 🐕 Hound)")
        except Exception as e:
            logger.warning(f"⚠️  AI initialization failed: {e}")
            self.gemini = None
            self.grok = None
            self.hound = None
            self.gemini_enabled = False
            self.grok_enabled = False
            self.hound_enabled = False

        # Initialize Congressional Trading Tracker
        # Internal state flags
        self._morning_flash_notified = False
        self._midday_flash_notified = False
        try:
            from congressional_tracker import CongressionalTracker
            self.congressional = CongressionalTracker(db_path=str(DB_PATH))
            self.congressional_enabled = True
            self._congressional_scan_cycle = 0   # Scan every N cycles (not every cycle)
            self._congressional_scan_interval = 4  # Every ~60 min (4 × 15-min cycles)
            logger.info("🏛️ Congressional Trading Tracker initialized")
        except Exception as e:
            logger.warning(f"⚠️  Congressional tracker init failed: {e}")
            self.congressional = None
            self.congressional_enabled = False

        # Initialize FRED Macro Tracker
        try:
            from fred_macro import FREDMacroTracker
            self.fred = FREDMacroTracker(db_path=str(DB_PATH))
            self.fred_enabled = self.fred.api_key is not None
            self._fred_scan_cycle = 0
            self._fred_scan_interval = 4   # Every ~60 min (data updates slowly)
            if self.fred_enabled:
                logger.info("📊 FRED Macro Tracker initialized (API key found)")
            else:
                logger.info("📊 FRED Macro Tracker initialized (no API key - add fred_api_key to config)")
        except Exception as e:
            logger.warning(f"⚠️  FRED macro tracker init failed: {e}")
            self.fred = None
            self.fred_enabled = False

        self.previous_defcon = self._load_last_defcon()
        self.monitor.previous_defcon = self.previous_defcon  # sync step-limiter
        self.monitoring_cycles = 0
        self.alerts_sent = 0
        self.pending_trade_alerts = []
        self.pending_trade_exits = []
        self._new_interval = None  # Set by /interval command
        self._daily_briefing_date = None  # Track last briefing date
        self._acquisition_pipeline_date = None  # Track last research+analyst run
        self._pipeline_runs = set()       # Track intraday checkpoint runs (pre-market, mid-day)
        self._morning_flash_date = None   # Track morning Flash briefing (9:30 AM)
        self._midday_flash_date  = None   # Track midday Flash briefing (12:00 PM)
        self._health_check_date  = None   # Track twice-weekly health check (Mon + Thu)
        self._verifier_cycle    = 0       # Counts cycles toward next conditional verification run
        self._verifier_interval = 4       # Normal: every ~60 min (4 × 15-min cycles); DEFCON 1-2: every cycle
        self._last_flash_forecast = None  # DEFCON forecast from latest flash briefing (1-5 or None)
        self._last_pending_pipeline_check = None  # Throttle opportunistic acquisition queue drains

        # Initialize real-time Alpaca WebSocket price stream
        try:
            from alpaca_stream import RealtimeMonitor
            self.realtime_monitor = RealtimeMonitor(
                broker=self.broker,
                paper_trading=self.paper_trading,
            )
            self.realtime_enabled = True
            logger.info("🔴 Real-time stream initialized (Alpaca WebSocket)")
        except Exception as e:
            logger.warning(f"⚠️  Real-time stream init failed: {e}")
            self.realtime_monitor = None
            self.realtime_enabled = False

        # Initialize Day Trader (Grok-powered intraday module)
        try:
            from day_trader import DayTrader
            self.day_trader = DayTrader(
                db_path=str(DB_PATH),
                paper_trading=self.paper_trading,
                alerts=self.alerts,
                realtime_monitor=getattr(self, 'realtime_monitor', None),
            )
            self.day_trader_enabled = True
            logger.info("🌅 Day Trader module initialized (Grok-powered)")
        except Exception as e:
            logger.warning(f"⚠️  Day Trader init failed: {e}")
            self.day_trader = None
            self.day_trader_enabled = False

        # Slash command processor
        self.cmd_processor = CommandProcessor(self)

        logger.info("✅ Orchestrator initialized successfully")
        logger.info(f"🤖 Broker Mode: {broker_mode.upper()}")
        logger.info(f"📰 News Monitoring: {'ENABLED' if self.news_enabled else 'DISABLED'}")
        logger.info(f"🔴 Real-time Stream: {'ENABLED' if self.realtime_enabled else 'DISABLED'}")
        logger.info(f"🌅 Day Trader: {'ENABLED' if self.day_trader_enabled else 'DISABLED'}")
        logger.info(f"📡 Slash commands: python3 hightrade_cmd.py /help")

        # Expose orchestrator instance for inter-module signaling (pending alerts)
        try:
            global ORCH_INSTANCE
            ORCH_INSTANCE = self
        except Exception:
            pass

        # Import any pending alerts written to disk by broker (durable fallback)
        try:
            import json
            from pathlib import Path
            pending_file = Path(__file__).parent / 'trading_data' / 'pending_alerts.json'
            if pending_file.exists():
                try:
                    with open(pending_file, 'r') as pf:
                        alerts = json.load(pf)
                    for a in alerts:
                        self.pending_trade_alerts.append(a)
                    # Remove the file after ingest
                    pending_file.unlink(missing_ok=True)
                    logger.info(f"🔁 Ingested {len(alerts)} pending alerts from disk into orchestrator queue")
                except Exception as _e:
                    logger.warning(f"Failed to ingest pending alerts file: {_e}")
        except Exception:
            pass

    # ── DEFCON persistence across restarts ────────────────────────────────
    def _load_last_defcon(self) -> int:
        """Load last known DEFCON from DB so restarts don't trigger phantom buys.
        Falls back to 5 (safe/no-trade) if no history exists."""
        conn = None
        try:
            from trading_db import get_sqlite_conn
            conn = get_sqlite_conn(str(DB_PATH), timeout=5)
            # Most recent monitoring point has the current DEFCON
            row = conn.execute(
                "SELECT defcon_level FROM signal_monitoring "
                "ORDER BY monitor_id DESC LIMIT 1"
            ).fetchone()
            if row and row[0] is not None:
                level = int(row[0])
                logger.info(f"📋 Restored previous DEFCON from DB: {level}")
                return level
        except Exception as e:
            logger.warning(f"⚠️  Could not load last DEFCON: {e}")
        finally:
            if conn:
                conn.close()
        logger.info("📋 No prior DEFCON found - defaulting to 5 (safe)")
        return 5

    def check_system_health(self):
        """Verify database and configuration are ready"""
        logger.info("\n" + "="*60)
        logger.info("SYSTEM HEALTH CHECK")
        logger.info("="*60)

        # Check database
        if not DB_PATH.exists():
            logger.error(f"❌ Database not found: {DB_PATH}")
            return False
        logger.info(f"✅ Database found: {DB_PATH}")

        # Check database connection
        try:
            self.monitor.connect()
            self.monitor.cursor.execute("SELECT COUNT(*) FROM crisis_events")
            crisis_count = self.monitor.cursor.fetchone()[0]
            self.monitor.disconnect()
            logger.info(f"✅ Database connected, {crisis_count} crises loaded")
        except Exception as e:
            logger.error(f"❌ Database connection failed: {e}")
            return False

        # Check alerts configuration
        if not self.alerts.config['channels']['email']['enabled']:
            logger.warning("⚠️  Email alerts are disabled")
            logger.warning("   To enable: Configure Gmail SMTP credentials")
        else:
            logger.info("✅ Email alerts enabled")

        logger.info("="*60)
        return True

    def setup_email_alerts(self):
        """Interactive setup for email alerts"""
        logger.info("\n" + "="*60)
        logger.info("EMAIL ALERTS SETUP")
        logger.info("="*60)

        print("\n📧 HighTrade Email Alert Configuration")
        print("=" * 50)
        print("\nThis tool helps you configure Gmail SMTP for alerts.")
        print("\n⚠️  IMPORTANT: Use Gmail App Password, not your regular password!")
        print("   Steps to get App Password:")
        print("   1. Enable 2-Step Verification on your Gmail account")
        print("   2. Go to myaccount.google.com/apppasswords")
        print("   3. Generate an app password for 'Mail' and 'Windows'")
        print("   4. Copy the generated 16-character password")

        email = input("\n📧 Enter your Gmail address: ").strip()
        if not email:
            logger.warning("Email setup cancelled")
            return False

        password = input("🔑 Enter your Gmail App Password (16 chars): ").strip()
        if not password or len(password) < 15:
            logger.error("Invalid app password")
            return False

        # Update configuration
        self.alerts.config['channels']['email']['enabled'] = True
        self.alerts.config['channels']['email']['username'] = email
        self.alerts.config['channels']['email']['password'] = password
        self.alerts.config['channels']['email']['address'] = email

        # Set alert thresholds
        self.alerts.config['alert_thresholds']['defcon_2'] = True
        self.alerts.config['alert_thresholds']['defcon_1'] = True

        self.alerts.save_config()
        logger.info(f"✅ Email alerts configured for {email}")

        # Test email
        print("\n🧪 Testing email configuration...")
        test_result = self.alerts.send_email(
            subject="HighTrade Test Alert",
            message="If you see this, email alerts are working correctly!",
            defcon_level=3
        )

        if test_result:
            logger.info("✅ Test email sent successfully!")
            return True
        else:
            logger.warning("⚠️  Test email failed. Check credentials.")
            return False

    def setup_slack_alerts(self):
        """Interactive setup for Slack webhook notifications"""
        logger.info("\n" + "="*60)
        logger.info("SLACK ALERTS SETUP")
        logger.info("="*60)

        print("\n🔗 HighTrade Slack Integration Setup")
        print("=" * 50)
        print("\nThis tool helps you configure Slack webhooks for trading alerts.")
        print("\n📖 Steps to get your Slack Webhook URL:")
        print("   1. Go to: https://api.slack.com/apps")
        print("   2. Click 'Create New App' → 'From scratch'")
        print("   3. Name: 'HighTrade Broker' and select your workspace")
        print("   4. Click 'Incoming Webhooks' in the left sidebar")
        print("   5. Toggle 'Activate Incoming Webhooks' to ON")
        print("   6. Click 'Add New Webhook to Workspace'")
        print("   7. Select the channel for notifications (e.g., #trading)")
        print("   8. Click 'Allow'")
        print("   9. Copy the Webhook URL")

        webhook_url = input("\n🔗 Paste your Slack Webhook URL: ").strip()
        if not webhook_url:
            logger.warning("Slack setup cancelled")
            return False

        if not webhook_url.startswith('https://hooks.slack.com/'):
            logger.error("❌ Invalid webhook URL. Must start with https://hooks.slack.com/")
            return False

        # Update configuration
        self.alerts.config['channels']['slack']['enabled'] = True
        self.alerts.config['channels']['slack']['webhook_url'] = webhook_url

        # Set alert thresholds for Slack
        self.alerts.config['alert_thresholds']['defcon_2'] = True
        self.alerts.config['alert_thresholds']['defcon_1'] = True

        self.alerts.save_config()
        logger.info(f"✅ Slack webhook configured")

        # Test Slack
        print("\n🧪 Testing Slack configuration...")
        test_result = self.alerts.send_slack(
            message="If you see this in Slack, webhook notifications are working correctly! 🚀",
            defcon_level=2
        )

        if test_result:
            logger.info("✅ Test message sent to Slack successfully!")
            logger.info("✅ Your broker will now send all trading notifications to Slack!")
            print("\n💡 Tip: Start the broker with: python3 hightrade_orchestrator.py continuous --broker semi_auto")
            return True
        else:
            logger.warning("⚠️  Test message failed. Check webhook URL and try again.")
            logger.info("🔧 Troubleshooting:")
            logger.info("   • Verify webhook URL is correct")
            logger.info("   • Check that webhook is not expired")
            logger.info("   • Make sure you have access to the Slack channel")
            return False

    def _enrich_positions_with_live_prices(self, positions: list) -> list:
        """
        Fetch the current market price for each open position and compute
        unrealized P&L. Updates trade_records in the DB and returns enriched list.
        Prefers real-time WebSocket prices when available; falls back to yfinance.
        Falls back gracefully - a price fetch failure never blocks the cycle.
        """
        if not positions:
            return positions

        import sqlite3 as _sqlite3
        from datetime import datetime as _dt

        enriched = []
        for pos in positions:
            p = dict(pos)
            sym = p.get('asset_symbol', '')
            entry_price = p.get('entry_price', 0) or 0
            shares = p.get('shares', 0) or 0

            try:
                current_price = None

                # Prefer real-time WebSocket price (sub-second freshness)
                if self.realtime_enabled and self.realtime_monitor:
                    current_price = self.realtime_monitor.get_price(sym)

                # Fallback to yfinance if stream doesn't have it
                if not current_price:
                    import yfinance as yf
                    ticker = yf.Ticker(sym)
                    hist = ticker.history(period='1d', interval='1m')
                    if not hist.empty:
                        current_price = float(hist['Close'].iloc[-1])
                    else:
                        # Market closed - use last close
                        hist = ticker.history(period='5d')
                        current_price = float(hist['Close'].iloc[-1]) if not hist.empty else None

                if current_price:
                    cost_basis = entry_price * shares
                    market_value = current_price * shares
                    upnl_dollars = market_value - cost_basis
                    upnl_pct = (upnl_dollars / cost_basis * 100) if cost_basis else 0

                    p['current_price'] = round(current_price, 2)
                    p['unrealized_pnl_dollars'] = round(upnl_dollars, 2)
                    p['unrealized_pnl_percent'] = round(upnl_pct, 2)
                    # Also fix position_size_dollars to true cost basis (entry * shares)
                    p['position_size_dollars'] = round(cost_basis, 2)

                    # Persist to DB - also advance peak_price (high-watermark for trailing stop)
                    try:
                        conn = _sqlite3.connect(str(self.paper_trading.db_path))
                        conn.execute("""
                            UPDATE trade_records
                            SET current_price = ?, unrealized_pnl_dollars = ?,
                                unrealized_pnl_percent = ?, last_price_updated = ?,
                                position_size_dollars = entry_price * shares,
                                peak_price = MAX(COALESCE(peak_price, entry_price), ?)
                            WHERE trade_id = ? AND status = 'open'
                        """, (current_price, upnl_dollars, upnl_pct,
                              _dt.now().isoformat(), current_price, p.get('trade_id')))
                        conn.commit()
                        conn.close()
                    except Exception:
                        pass

            except Exception as e:
                logger.warning(f"Price fetch failed for {sym}: {e}")

            enriched.append(p)
        return enriched

    def run_monitoring_cycle(self):
        """Execute one monitoring cycle with alerts"""
        self.monitoring_cycles += 1

        logger.info(f"\n{'='*60}")
        logger.info(f"MONITORING CYCLE #{self.monitoring_cycles}")
        logger.info(f"{'='*60}")

        # --- CORE DATA GATHERING (Start of Cycle) ---
        logger.debug(f"DEBUG: Current PATH: {os.environ.get('PATH')}")
        sector_result = None
        vix_result = None
        macro_result = None

        if self.sector_analyzer:
            sector_result = self.sector_analyzer.get_rotation_data()

        if self.vix_analyzer:
            vix_result = self.vix_analyzer.get_term_structure_data()

        try:
            self.monitor.connect()

            # Run full monitoring cycle (includes fallback data handling)
            logger.info("📊 Fetching real-time market data...")

            # Fetch real-time data with fallback
            yield_data = self.monitor.fetch_bond_yield()
            vix_data = self.monitor.fetch_vix()
            market_data = self.monitor.fetch_market_prices()

            # Use simulated data as fallback
            data_source = "REAL"
            if not yield_data or not vix_data or not market_data:
                data_source = "SIMULATED (API Unavailable)"
                sim_data = self.monitor.get_simulated_data()
                if not yield_data:
                    yield_data = sim_data['yield_data']
                if not vix_data:
                    vix_data = sim_data['vix_data']
                if not market_data:
                    market_data = sim_data['market_data']

            logger.info(f"  📡 Data Source: {data_source}")

            # Log fetched data
            if yield_data:
                logger.info(f"  ✅ Bond Yield (10Y): {yield_data['yield']:.2f}%")
            if vix_data:
                logger.info(f"  ✅ VIX Index: {vix_data['vix']:.2f}")
            if market_data:
                logger.info(f"  ✅ S&P 500: {market_data['change_pct']:+.2f}% change")

            # NEW: Fetch and analyze news
            news_signal = None
            logger.info(f"DEBUG: self.news_enabled = {self.news_enabled}")
            if self.news_enabled:
                logger.info("📰 Checking news sources...")
                # FIRST: Check database for active breaking news
                breaking_db_signal = self._check_breaking_news_in_db()
                if breaking_db_signal:
                    logger.warning(f"  🔥 Using breaking news from database: {breaking_db_signal['crisis_description']}")
                    news_signal = breaking_db_signal

                # THEN: Fetch fresh news from APIs
                try:
                    logger.info("📰 Fetching and analyzing news...")
                    articles = self.news_aggregator.fetch_latest_news(lookback_hours=1)
                    logger.info(f"  📰 Fetched {len(articles)} news articles from all sources")

                    if articles:
                        fresh_news_signal = self.news_signal_gen.generate_news_signal(articles, self.news_sentiment)
                        score = fresh_news_signal['news_score']
                        logger.info(f"  📊 News Score: {score:.1f}/100")
                        logger.info(f"  📰 Crisis Type: {fresh_news_signal['dominant_crisis_type']}")
                        logger.info(f"  📰 Sentiment: {fresh_news_signal['sentiment_summary']}")
                        components = fresh_news_signal.get('score_components', {})
                        if components:
                            logger.info(f"  📊 Components: sentiment={components.get('sentiment_net',0):.1f} concentration={components.get('signal_concentration',0):.1f} urgency={components.get('urgency_premium',0):.1f}")

                        # Always use fresh news signal so news_score flows to signal calc + recording.
                        # calculate_defcon_level guards breaking override internally.
                        news_signal = fresh_news_signal
                        if fresh_news_signal['breaking_news_override']:
                            logger.warning(f"  🚨 BREAKING NEWS DETECTED: {fresh_news_signal['crisis_description']}")

                        # Detect new articles BEFORE Gemini - skip LLM when 0 new articles
                        new_count, latest_articles = self._detect_new_news(fresh_news_signal)

                        # --- GEMINI LAYER 1: Flash analysis (skip if no new articles) ---
                        gemini_flash_result = None
                        gemini_pro_result = None
                        defcon_changed = (self.previous_defcon != self.monitor.defcon_level)
                        has_breaking = fresh_news_signal['breaking_news_override']
                        should_run_gemini = new_count > 0 or has_breaking or defcon_changed

                        if self.gemini_enabled and should_run_gemini:
                            # Reuse cached batch results from generate_news_signal (avoids redundant analyze_batch)
                            cached_results = fresh_news_signal.get('_batch_results', [])
                            articles_for_gemini = [
                                {
                                    'title': a.title,
                                    'description': a.description[:300] if a.description else '',
                                    'source': a.source,
                                    'published_at': a.published_at.isoformat(),
                                    'sentiment': getattr(r, 'sentiment', 'neutral'),
                                    'urgency': getattr(r, 'urgency', 'routine'),
                                    'confidence': getattr(r, 'confidence', 0),
                                    'matched_keywords': getattr(r, 'matched_keywords', [])
                                }
                                for a, r in zip(articles, cached_results)
                            ] if articles and cached_results else []

                            gemini_flash_result = self.gemini.run_flash_analysis(
                                articles_for_gemini,
                                score_components=components,
                                sentiment_summary=fresh_news_signal['sentiment_summary'],
                                crisis_type=fresh_news_signal['dominant_crisis_type'],
                                sector_rotation=sector_result,
                                vix_term_structure=vix_result
                            )

                            # --- LAYER 2: Grok deep analysis on elevated signals ---
                            if self.gemini.should_run_pro(score, fresh_news_signal['breaking_count'], defcon_changed):
                                logger.info(f"  🧠 Elevated signal ({score:.1f}) - triggering Grok deep analysis...")
                                open_positions = self.paper_trading.get_open_positions()

                                _pro_briefing_ctx = None
                                try:
                                    from broker_agent import get_latest_briefing_context
                                    _, _pro_briefing_ctx = get_latest_briefing_context(scope='risk')
                                except Exception:
                                    pass

                                gemini_pro_result = self.grok.run_deep_analysis(
                                    articles_for_gemini,
                                    score_components=components,
                                    sentiment_summary=fresh_news_signal['sentiment_summary'],
                                    crisis_type=fresh_news_signal['dominant_crisis_type'],
                                    news_score=score,
                                    flash_analysis=gemini_flash_result,
                                    current_defcon=self.previous_defcon,
                                    positions=open_positions,
                                    sector_rotation=sector_result,
                                    vix_term_structure=vix_result,
                                    briefing_context=_pro_briefing_ctx,
                                ) if self.grok_enabled else None
                        elif self.gemini_enabled:
                            logger.info(f"  ⏭️  Skipping Gemini - 0 new articles (reusing previous analysis)")

                        # Store full signal with Gemini Flash embedded
                        signal_id = self._record_news_signal(
                            fresh_news_signal,
                            articles_full=articles,
                            gemini_flash=gemini_flash_result
                        )

                        # Save Grok deep analysis to gemini_analysis table (same schema)
                        if gemini_pro_result and signal_id and self.grok_enabled:
                            self.gemini.save_analysis_to_db(
                                str(DB_PATH), signal_id, gemini_pro_result,
                                trigger_type='elevated' if score >= 40 else 'breaking'
                            )

                        if fresh_news_signal['article_count'] > 0:
                            # Extract dominant sentiment label
                            sentiment_text = fresh_news_signal['sentiment_summary']
                            if ':' in sentiment_text:
                                parts = sentiment_text.split(',')
                                sentiments = {}
                                for part in parts:
                                    if ':' in part:
                                        name, pct = part.split(':')
                                        sentiments[name.strip()] = float(pct.strip().rstrip('%'))
                                dominant = max(sentiments, key=sentiments.get).lower()
                            else:
                                dominant = sentiment_text.lower()

                            # Build AI summaries for Slack if available
                            gemini_summary = None
                            if gemini_flash_result:
                                gemini_summary = {
                                    'action': gemini_flash_result.get('recommended_action', 'WAIT'),
                                    'coherence': gemini_flash_result.get('narrative_coherence', 0),
                                    'confidence': gemini_flash_result.get('confidence_in_signal', 0),
                                    'theme': gemini_flash_result.get('dominant_theme', ''),
                                    'reasoning': gemini_flash_result.get('reasoning', '')[:200]
                                }

                            # Send silent notification to #logs-silent every cycle
                            self.alerts.send_silent_log('news_update', {
                                'news_score': score,
                                'crisis_type': fresh_news_signal['dominant_crisis_type'],
                                'sentiment': dominant,
                                'article_count': fresh_news_signal['article_count'],
                                'new_article_count': new_count,
                                'breaking_count': fresh_news_signal['breaking_count'],
                                'score_components': components,
                                'top_articles': latest_articles[:3],
                                'gemini': gemini_summary,
                                'timestamp': datetime.now().isoformat()
                            })

                            logger.info(f"  ✅ News notification sent to #logs-silent ({new_count} new, {fresh_news_signal['article_count']} total)")
                    else:
                        logger.info("  📰 No recent news articles found")

                except Exception as e:
                    logger.warning(f"  ⚠️  News fetch failed: {e} - continuing with quantitative only")
                    # Keep breaking_db_signal if we have it

            # ── Congressional Trading Tracker (every ~60 min) ──────────────
            congressional_result = None
            self._congressional_scan_cycle = getattr(self, '_congressional_scan_cycle', 0) + 1
            if (self.congressional_enabled and
                    self._congressional_scan_cycle >= self._congressional_scan_interval):
                self._congressional_scan_cycle = 0
                logger.info("🏛️ Running congressional trading scan...")
                try:
                    congressional_result = self.congressional.run_full_scan(days_back=30)
                    if congressional_result.get('has_clusters'):
                        top = congressional_result['clusters'][0]
                        logger.info(
                            f"  🎯 TOP CLUSTER: {top['ticker']} - "
                            f"{top['buy_count']} politicians, strength={top['signal_strength']:.0f}, "
                            f"bipartisan={top['bipartisan']}"
                        )
                        # Send Slack alert for strong cluster signals
                        if top['signal_strength'] >= 50:
                            self.alerts.send_silent_log('congressional_cluster', {
                                'ticker': top['ticker'],
                                'buy_count': top['buy_count'],
                                'politicians': top['politicians'][:5],
                                'bipartisan': top['bipartisan'],
                                'committee_relevance': top.get('committee_relevance', []),
                                'signal_strength': top['signal_strength'],
                                'total_amount': top.get('total_estimated_amount', 0),
                                'window_days': top.get('window_days', 30)
                            })
                    else:
                        logger.info(f"  🏛️ {congressional_result['significant_trades']} significant trades, no clusters detected")
                except Exception as e:
                    logger.warning(f"  ⚠️ Congressional scan failed: {e}")
            elif self.congressional_enabled:
                logger.debug(f"  🏛️ Congressional scan: {self._congressional_scan_interval - self._congressional_scan_cycle} cycles until next scan")

            # ── FRED Macro Tracker (every ~60 min) ────────────────────────
            macro_result = None
            self._fred_scan_cycle = getattr(self, '_fred_scan_cycle', 0) + 1
            if self.fred_enabled and self._fred_scan_cycle >= self._fred_scan_interval:
                try:
                    macro_result = self.fred.run_full_analysis()
                    self.fred.save_to_db(macro_result)
                    macro_score = macro_result.get('macro_score', 50)
                    defcon_mod = macro_result.get('defcon_modifier', 0)
                    bearish = macro_result.get('bearish_count', 0)
                    logger.info(f"  📊 Macro Score: {macro_score:.0f}/100 | DEFCON modifier: {defcon_mod:+.1f} | Bearish signals: {bearish}")

                    # Send Slack macro update if there are noteworthy signals
                    if bearish >= 2 or macro_score < 35:
                        self.alerts.send_silent_log('macro_update', {
                            'macro_score': macro_score,
                            'defcon_modifier': defcon_mod,
                            'bearish_count': bearish,
                            'bullish_count': macro_result.get('bullish_count', 0),
                            'signals': macro_result.get('macro_signals', []),
                            'yield_curve': macro_result.get('data', {}).get('yield_curve_spread'),
                            'fed_funds': macro_result.get('data', {}).get('fed_funds_rate'),
                            'unemployment': macro_result.get('data', {}).get('unemployment_rate'),
                            'hy_oas_bps': macro_result.get('data', {}).get('hy_oas_bps')
                        })
                except Exception as e:
                    logger.warning(f"  ⚠️ FRED macro scan failed: {e}")
            elif self.fred_enabled:
                # Pull latest from DB for DEFCON calculation even if not scanning
                try:
                    macro_result = self.fred.get_latest_from_db()
                    if macro_result:
                        macro_result = {
                            'available': True,
                            'macro_score': macro_result.get('macro_score', 50),
                            'defcon_modifier': macro_result.get('defcon_modifier', 0)
                        }
                except Exception:
                    pass

            # ── Grok Hound (every ~60 min or force-trigger file) ─────────
            _force_hound = Path(DB_PATH).parent / ".force_hound"
            _hound_forced = _force_hound.exists()
            if _hound_forced:
                _force_hound.unlink(missing_ok=True)
                logger.info("🐕 Force-trigger detected — running Grok Hound now")
            self._hound_scan_cycle = getattr(self, '_hound_scan_cycle', 0) + 1
            if self.hound_enabled and (_hound_forced or self._hound_scan_cycle >= self._hound_scan_interval):
                self._hound_scan_cycle = 0
                logger.info("🐕 Running Grok Hound hourly alpha scan...")
                try:
                    open_pos_tickers = [p.get('asset_symbol') for p in self.paper_trading.get_open_positions()]
                    _news_crisis = news_signal.get('dominant_crisis_type') if news_signal else None
                    _financing_alert = _news_crisis == 'low_float_financing'
                    _reverse_split_alert = _news_crisis == 'reverse_split_low_float'
                    _pipeline_deal_alert = _news_crisis == 'pipeline_deal_boost'
                    for _flag, _label in [
                        (_financing_alert, 'low_float_financing'),
                        (_reverse_split_alert, 'reverse_split_low_float'),
                        (_pipeline_deal_alert, 'pipeline_deal_boost'),
                    ]:
                        if _flag:
                            logger.info(f"  💉 Cross-feed: {_label} news detected — flagging Grok Hound for velocity scan")
                    hound_state = {
                        "defcon_level": self.monitor.defcon_level,
                        "macro_score": macro_result.get('macro_score', 50) if macro_result else 50,
                        "watchlist": open_pos_tickers,
                        "latest_gemini_briefing_summary": "",
                        "low_float_financing_alert": _financing_alert,
                        "reverse_split_alert": _reverse_split_alert,
                        "pipeline_deal_alert": _pipeline_deal_alert
                    }
                    hound_results = self.hound.hunt(hound_state, focus_tickers=open_pos_tickers)
                    self.hound.save_candidates(hound_results)

                    # Alert Slack ONLY for elite alpha (score >= 75)
                    for candidate in hound_results.get('candidates', []):
                        if candidate.get('alpha_score', 0) >= 75:
                            self.alerts.send_notify('hound_alert', {
                                'ticker': candidate['ticker'],
                                'score': candidate['alpha_score'],
                                'thesis': candidate['why_next'],
                                'risks': candidate['risks'],
                                'action': candidate['action_suggestion']
                            })

                    # Trigger pipeline - researcher picks up new pending items,
                    # analyst ALWAYS runs to catch any library_ready items waiting
                    try:
                        from acquisition_researcher import run_research_cycle
                        from acquisition_analyst import run_analyst_cycle
                        researched = run_research_cycle()
                        # Generate sector context for analyst
                        _sector_ctx = ''
                        try:
                            _crisis_type = news_signal.get('dominant_crisis_type', 'market_correction') if news_signal else 'market_correction'
                            _sc = self.sector_analyzer.get_sector_context(
                                crisis_type=_crisis_type,
                                defcon_level=self.monitor.defcon_level,
                                is_winding_down=getattr(self.monitor, 'is_winding_down', False),
                                deescalation_score=getattr(self, '_last_deesc_score', 0),
                            )
                            _sector_ctx = _sc.get('rotation_guidance', '')
                        except Exception as _se:
                            logger.warning(f"Sector context failed: {_se}")

                        run_analyst_cycle(extra_context={   # always - don't gate on researched
                            'defcon_level': self.monitor.defcon_level,
                            'news_score':   getattr(self, '_last_news_score', 0),
                            'is_winding_down': getattr(self.monitor, 'is_winding_down', False),
                            'deescalation_score': getattr(self, '_last_deesc_score', 0),
                            'sector_guidance': _sector_ctx,
                        })
                    except Exception as e:
                        logger.warning(f"  🔬 Pipeline auto-trigger failed: {e}")

                    n_found = len(hound_results.get('candidates', []))
                    logger.info(f"  🐕 Hound complete - {n_found} candidates, next run in ~{self._hound_scan_interval * 15} min")
                except Exception as e:
                    logger.warning(f"  🐕 Grok Hound failed: {e}")
            else:
                remaining = self._hound_scan_interval - self._hound_scan_cycle
                logger.debug(f"  🐕 Hound: {remaining} cycle(s) until next run")

            # Calculate and record
            logger.info("📈 Calculating signal scores...")
            _news_score = news_signal.get('news_score', 0) if news_signal else 0
            self._last_news_score = _news_score     # persist for pipeline calls mid-cycle
            self._last_deesc_score = news_signal.get('deescalation_score', 0) if news_signal else 0
            signal_scores = self.monitor.calculate_signal_scores(yield_data, vix_data, market_data, news_score=_news_score)
            # Fetch briefing signal quality for DEFCON nudge
            _briefing_sq = None
            try:
                from broker_agent import get_latest_briefing_context
                _br, _ = get_latest_briefing_context(scope='risk')
                _briefing_sq = _br.get('signal_quality')
            except Exception:
                pass

            _deesc_score = news_signal.get('deescalation_score', 0) if news_signal else 0

            current_defcon, signal_score = self.monitor.calculate_defcon_level(
                signal_scores, market_data, news_signal,
                flash_forecast=getattr(self, '_last_flash_forecast', None),
                macro_modifier=macro_result.get('defcon_modifier') if macro_result else None,
                briefing_signal_quality=_briefing_sq,
                deescalation_score=_deesc_score,
            )

            # Read wind-down state from monitor (set by step-limiting logic)
            _is_winding_down = getattr(self.monitor, 'is_winding_down', False)
            _wind_down_cycles = getattr(self.monitor, 'defcon_hold_cycles', 0)

            logger.info(f"  📊 Bond Yield Spike Score: {signal_scores.get('bond_yield_spike', 0):.1f}")
            logger.info(f"  📊 VIX Spike Score: {signal_scores.get('vix_spike', 0):.1f}")
            logger.info(f"  📊 Market Drawdown Score: {signal_scores.get('market_drawdown', 0):.1f}")
            logger.info(f"  📊 News Signal Score: {signal_scores.get('news_signal', 0):.1f}")
            logger.info(f"  📊 Composite Score: {signal_score:.1f}/100")
            if _deesc_score > 0:
                logger.info(f"  📊 De-escalation Score: {_deesc_score:.1f}/100")
            if _is_winding_down:
                logger.info(f"  🔄 Wind-down active (cycle {_wind_down_cycles})")

            # Record to database
            logger.info("💾 Recording to database...")
            result = self.monitor.record_monitoring_point(
                yield_data,
                vix_data,
                market_data,
                defcon_level=current_defcon,
                news_signal=news_signal,
                signal_score=signal_score
            )

            if not result:
                logger.warning("Failed to record monitoring point")
                return

            logger.info(f"✅ DEFCON Level: {current_defcon}/5")
            logger.info(f"✅ Signal Score: {signal_score:.1f}/100")
            self._last_defcon = current_defcon  # stored for exit_analyst context

            # Send alerts if DEFCON changed or escalated
            if current_defcon != self.previous_defcon:
                old_defcon = self.previous_defcon
                self.previous_defcon = current_defcon  # Always update - fixes de-escalation blindness

                if current_defcon < old_defcon:
                    logger.warning(f"🚨 DEFCON ESCALATION: {old_defcon} → {current_defcon}")
                else:
                    logger.info(f"🟢 DEFCON DE-ESCALATION: {old_defcon} → {current_defcon}")

                bond_yield = yield_data['yield'] if yield_data else None
                vix = vix_data['vix'] if vix_data else None
                market_change = market_data['change_pct'] if market_data else None

                alert_message = f"""
Market Conditions Alert

Previous DEFCON: {old_defcon}
Current DEFCON: {current_defcon}
Signal Score: {signal_score:.1f}/100

Bond Yield: {bond_yield}%
VIX: {vix}
Market Change: {market_change}%

Check dashboard for detailed analysis.
                """.strip()

                self.alerts.send_defcon_alert(
                    defcon_level=current_defcon,
                    signal_score=signal_score,
                    details=alert_message
                )
                self.alerts_sent += 1

                # Also log to silent channel
                self.alerts.send_silent_log('defcon_change', {
                    'old_defcon': old_defcon,
                    'new_defcon': current_defcon,
                    'signal_score': signal_score
                })

                # Log wind-down transition if active
                if _is_winding_down:
                    self.alerts.send_silent_log('wind_down', {
                        'defcon': current_defcon,
                        'wind_down_cycles': _wind_down_cycles,
                        'deescalation_score': _deesc_score,
                    })

                # Broker agent decides on trades (DEFCON 1-3: crisis + dip buying)
                if self.cmd_processor.should_skip_trades:
                    logger.warning("⏸️  Trading on HOLD - skipping trade execution")
                elif current_defcon <= 3:
                    crisis_desc = f"DEFCON {current_defcon} escalation - Signal Score: {signal_score:.1f}"
                    market_conditions = {'vix': vix} if vix else {}

                    if self.broker_mode != 'disabled':
                        # Autonomous mode: broker makes decision
                        logger.info("\n" + "="*60)
                        logger.info("🤖 BROKER AGENT: Analyzing market conditions...")
                        logger.info("="*60)

                        trade_executed = self.broker.process_market_conditions(
                            defcon_level=current_defcon,
                            signal_score=signal_score,
                            crisis_description=crisis_desc,
                            market_data=market_conditions
                        )

                        if trade_executed:
                            logger.info("✅ BROKER: Buy executed autonomously!")
                        else:
                            logger.info("ℹ️  BROKER: No DEFCON basket buy executed - dynamic acquisition pipeline remains active")
                    else:
                        logger.info("ℹ️  Manual DEFCON basket alerts removed - acquisition pipeline is the active buy path")

                # Monitor and process exits (respects hold)
                if not self.cmd_processor.should_skip_trades:
                    if self.broker_mode == 'disabled':
                        self.monitor_and_exit_positions()
                    else:
                        exits = self.broker.process_exits()
                        if exits > 0:
                            logger.info(f"✅ BROKER: {exits} position(s) exited autonomously")
                        # Always check for positions missing exit frameworks, regardless of broker mode
                        self._check_positions_missing_exit_framework()

            else:
                logger.info("No DEFCON change - maintaining current status")
                # Still monitor positions even without DEFCON change
                if not self.cmd_processor.should_skip_trades:
                    if self.broker_mode == 'disabled':
                        self.monitor_and_exit_positions()
                    else:
                        exits = self.broker.process_exits()
                        if exits > 0:
                            logger.info(f"✅ BROKER: {exits} position(s) exited autonomously")
                        # Always check for positions missing exit frameworks, regardless of broker mode
                        self._check_positions_missing_exit_framework()

            # ── Acquisition conditionals (market hours only - Mon-Fri 9:30-16:00 ET) ──
            _now_et = _et_now()
            _market_open = (
                _now_et.weekday() < 5  # Mon-Fri only
                and (_now_et.hour > 9 or (_now_et.hour == 9 and _now_et.minute >= 30))
                and _now_et.hour < 16
            )
            if self.broker_mode != 'disabled' and _market_open:
                try:
                    live_state = {
                        'defcon': current_defcon,
                        'news_score': locals().get('score') or 0,
                        'macro_score': self._get_latest_macro_score(),
                        'is_winding_down': _is_winding_down,
                        'deescalation_score': _deesc_score,
                    }
                    acq_entries = self.broker.process_acquisition_conditionals(live_state=live_state)
                    if acq_entries > 0:
                        logger.info(f"🎯 BROKER: {acq_entries} acquisition conditional(s) entered")
                except Exception as acq_err:
                    logger.warning(f"Acquisition conditional check failed: {acq_err}")

            # Send silent log to #logs-silent channel
            try:
                status = self.monitor.get_status() or {}
                open_positions = self.paper_trading.get_open_positions()
                perf = self.paper_trading.get_portfolio_performance()

                # Fetch live prices and compute unrealized P&L for each open position
                open_positions = self._enrich_positions_with_live_prices(open_positions)

                # Calculate live portfolio value from Alpaca broker (real account values)
                total_capital = self.paper_trading.total_capital
                realized_pnl  = perf.get('total_profit_loss_dollars', 0)
                unrealized_pnl = sum(p.get('unrealized_pnl_dollars') or 0 for p in open_positions)
                deployed = sum(
                    (p.get('current_price') or p.get('entry_price', 0)) * p.get('shares', 0)
                    for p in open_positions
                )
                # Use real Alpaca account equity/cash if available; fall back to DB-computed values
                alpaca_snapshot = self.paper_trading._get_alpaca_account_snapshot()
                if alpaca_snapshot and alpaca_snapshot.get('equity', 0) > 0:
                    # Alpaca equity reflects only the broker deposit; paper trades are
                    # tracked locally in the DB and not mirrored to Alpaca. Add
                    # DB-computed P&L to get the true account value.
                    account_value  = alpaca_snapshot['equity'] + realized_pnl + unrealized_pnl
                    cash_available = alpaca_snapshot['cash'] + realized_pnl - deployed
                else:
                    account_value  = total_capital + realized_pnl + unrealized_pnl
                    cash_available = total_capital + realized_pnl - sum(
                        p.get('entry_price', 0) * p.get('shares', 0) for p in open_positions
                    )

                self.alerts.send_silent_log('monitoring_cycle', {
                    'cycle': self.monitoring_cycles,
                    'defcon_level': status.get('defcon_level', 5),
                    'signal_score': status.get('signal_score', 0),
                    'vix': status.get('vix', '?'),
                    'bond_yield': status.get('bond_yield', '?'),
                    'open_positions': open_positions,
                    'total_capital': total_capital,
                    'account_value': account_value,
                    'cash_available': cash_available,
                    'deployed': deployed,
                    'realized_pnl': realized_pnl,
                    'unrealized_pnl': unrealized_pnl,
                    'total_pnl_pct': perf.get('total_profit_loss_percent', 0),
                    'win_rate': perf.get('win_rate', 0),
                    'open_trades': perf.get('open_trades', 0),
                    'closed_trades': perf.get('closed_trades', 0),
                })
            except Exception as log_err:
                # Don't let logging errors break the cycle
                pass

        except Exception as e:
            logger.error(f"Error in monitoring cycle: {e}", exc_info=True)
        finally:
            self.monitor.disconnect()

        # ── Day Trader Checkpoints (scan, buy, stop/TP, EOD exit) ────────
        if self.day_trader_enabled and self.day_trader and self.day_trader._enabled:
            try:
                self.day_trader.check_premarket_scan()
                self.day_trader.check_market_open_buy()
                self.day_trader.check_intraday_exits()
                self.day_trader.check_eod_exit()
            except Exception as e:
                logger.warning(f"  ⚠️ Day Trader cycle failed: {e}")

        # ── Intraday Flash Briefings (morning 9:30 AM, midday 12:00 PM) ───
        self._check_flash_briefings()

        # ── Conditional Verifier (hourly normal · every cycle at DEFCON 1-2) ────────
        self._verifier_cycle += 1
        _v_interval = 1 if self.previous_defcon <= 2 else self._verifier_interval
        if self._verifier_cycle >= _v_interval:
            self._verifier_cycle = 0
            _v_mode = 'HIGH-ALERT (15 min)' if self.previous_defcon <= 2 else 'hourly'
            logger.info(f"🔍 Conditional verifier firing [{_v_mode}] - DEFCON {self.previous_defcon}/5")
            try:
                from acquisition_verifier import run_verification_cycle
                summary     = run_verification_cycle()
                confirmed   = summary.get('confirmed',   0)
                flagged     = summary.get('flagged',     0)
                invalidated = summary.get('invalidated', 0)
                corrected   = summary.get('corrected',   0)
                demoted     = summary.get('demoted',     0)
                archived    = summary.get('archived',    0)
                logger.info(
                    f"  ✅ Verifier: confirmed={confirmed}, flagged={flagged}, "
                    f"corrected={corrected}, demoted={demoted}, "
                    f"archived={archived}, invalidated={invalidated}"
                )
                _v_payload = {
                    'confirmed':   confirmed,
                    'flagged':     flagged,
                    'invalidated': invalidated,
                    'corrected':   corrected,
                    'demoted':     demoted,
                    'archived':    archived,
                    'defcon':      self.previous_defcon,
                    'mode':        _v_mode,
                }
                if flagged or invalidated or corrected or demoted or archived:
                    # Push notify - thesis changed, corrected, demoted, or killed
                    self.alerts.send_notify('verifier_alert', _v_payload)
                self.alerts.send_silent_log('verifier_alert', _v_payload)
            except Exception as e:
                logger.warning(f"  ⚠️ Conditional verifier failed: {e}")

        # ── Acquisition Pipeline Checkpoints (pre-market 9:00 AM, mid-day 12:30 PM) ─
        self._check_acquisition_pipeline()
        self._check_pending_acquisition_work()

        # ── Daily Briefing (fires once per day at/after 4:30 PM) ──────────
        self._check_daily_briefing()

        # ── Bi-weekly Health Check (Thursdays only, once per week pair) ───
        self._check_health_agent()

    def _check_health_agent(self):
        """
        Twice-weekly system health check - fires on Mondays and Thursdays,
        throttled to at most once per 3 days by health_agent's internal state.
        Checks APIs, monitoring recency, recurring data gaps, and new Gemini models.
        Results sent to #all-hightrade via send_notify().
        """
        now = _et_now()
        # Only run on Monday (0) or Thursday (3) - ET calendar day
        if now.weekday() not in (0, 3):
            return
        today = now.strftime('%Y-%m-%d')
        if self._health_check_date == today:
            return  # already ran today
        self._health_check_date = today

        day_name = 'Monday' if now.weekday() == 0 else 'Thursday'
        logger.info(f"🏥 {day_name} health check - running twice-weekly system audit...")
        try:
            from health_agent import run_and_notify
            result = run_and_notify(self.alerts, force=False)
            if result:
                logger.info(f"  ✅ Health check complete: {result.get('summary', '')}")
            # 'skipped' means <3 days since last run - silently move on
        except Exception as e:
            logger.warning(f"  ⚠️  Health agent failed: {e}")

    def _check_flash_briefings(self):
        """
        Fire lightweight Gemini Flash briefings at two intraday checkpoints:
          • Morning  - 9:30 AM ET  (market open snapshot)
          • Midday   - 12:00 PM ET (lunch check-in)

        Each fires once per calendar day. Results saved to daily_briefings table
        (model_key = 'morning_flash' / 'midday_flash') so the 4:30 PM Pro synthesis
        has structured intraday context. Summary also sent to #logs-silent.
        """
        now_et   = _et_now()                      # Eastern Time for market schedule
        today = now_et.strftime('%Y-%m-%d')

        # Local time for user-facing morning flash scheduling
        from datetime import datetime as _dt, timedelta as _td
        now_local_dt = _dt.now()
        local_hour = now_local_dt.hour
        local_minute = now_local_dt.minute

        windows = [
            ('morning', 9,  30,  '_morning_flash_date', '🌅 Morning'),
            ('morning_notify', 8, 0, '_morning_flash_notified', '🔔 Morning Notify'),
            ('midday',  12,  0,  '_midday_flash_date',  '☀️  Midday'),
        ]

        GRACE_MINUTES = 90
        for label, tgt_hour, tgt_min, attr, emoji in windows:
            # Default: use ET-based now/minutes
            now_minutes = now_et.hour * 60 + now_et.minute
            target_minutes = tgt_hour * 60 + tgt_min

            # For the morning flash, schedule relative to LOCAL time (target = tgt_time - 120m local)
            if label == 'morning':
                # Compute local target as (tgt_hour:tgt_min) minus 120 minutes in local day
                local_target_minutes = ((tgt_hour * 60 + tgt_min) - 120) % (24 * 60)
                now_minutes = local_hour * 60 + local_minute
                target_minutes = local_target_minutes

            past_window = (now_minutes >= target_minutes and now_minutes < target_minutes + GRACE_MINUTES)
            already_ran = getattr(self, attr) == today
            if not past_window or already_ran:
                continue

            # DB guard: survive orchestrator restarts - don't re-fire if already in DB today
            if not already_ran:
                try:
                    import sqlite3 as _sq2
                    _c = _sq2.connect(str(DB_PATH))
                    _hit = _c.execute(
                        "SELECT 1 FROM daily_briefings WHERE date=? AND model_key=? LIMIT 1",
                        (today, f'{label}_flash')
                    ).fetchone()
                    _c.close()
                    if _hit:
                        setattr(self, attr, today)  # stamp in-memory
                        logger.debug(f"  ⏭️  {emoji} flash briefing already in DB for {today} - skipping")
                        continue
                except Exception:
                    pass  # If DB check fails, fall through and let the normal guard handle it

            logger.info(f"📊 {emoji} Flash briefing firing ({tgt_hour:02d}:{tgt_min:02d})...")
            try:
                # If this is the morning_notify window, fetch the already-saved morning flash and send the user-facing notification
                if label == 'morning_notify':
                    try:
                        import sqlite3 as _sq3
                        conn = _sq3.connect(str(DB_PATH))
                        conn.row_factory = _sq3.Row
                        row = conn.execute(
                            "SELECT headline_summary FROM daily_briefings WHERE date=? AND model_key='morning_flash' LIMIT 1",
                            (today,)
                        ).fetchone()
                        conn.close()
                        if row and row['headline_summary']:
                            # send notification (short headline) and mark notified
                            summary = row['headline_summary']
                            self.alerts.send_slack(f"🌅 Morning Briefing: {summary}", defcon_level=3)
                            setattr(self, attr, today)
                        else:
                            logger.info("  ⏭️ No morning_flash found in DB to notify")
                    except Exception as _e:
                        logger.warning(f"  ⚠️ Morning notify DB fetch failed: {_e}")
                else:
                    self._run_flash_briefing(label, emoji)
                    setattr(self, attr, today)   # only stamp date on success - enables retry next cycle on failure
            except Exception as e:
                logger.warning(f"{emoji} Flash briefing failed - will retry next cycle: {e}")

    def _run_flash_briefing(self, label: str, emoji: str):
        """Build a concise Flash prompt from live state and send summary to #logs-silent."""
        import sqlite3 as _sq

        # ── Gather context ────────────────────────────────────────────────
        now_str = _et_now().strftime('%Y-%m-%d %H:%M ET')

        # Latest news signal from DB
        try:
            conn = _sq.connect(str(DB_PATH))
            conn.row_factory = _sq.Row
            row = conn.execute("""
                SELECT news_score, dominant_crisis_type, sentiment_summary,
                       crisis_description, breaking_news_override
                FROM news_signals ORDER BY timestamp DESC LIMIT 1
            """).fetchone()
            conn.close()
            if row:
                news_ctx = (
                    f"Score: {row['news_score']:.1f}/100 | "
                    f"Type: {row['dominant_crisis_type']} | "
                    f"Sentiment: {row['sentiment_summary']} | "
                    f"Breaking: {'YES' if row['breaking_news_override'] else 'no'}\n"
                    f"Description: {row['crisis_description']}"
                )
            else:
                news_ctx = "No recent news signal available."
        except Exception:
            news_ctx = "News signal unavailable."

        # Open positions - enriched with live prices and unrealized P&L
        try:
            open_positions = self.paper_trading.get_open_positions()
            open_positions = self._enrich_positions_with_live_prices(open_positions)
            pos_lines = []
            for p in open_positions:
                sym        = p['asset_symbol']
                shares     = p.get('shares', 0)
                entry      = p.get('entry_price', 0)
                current    = p.get('current_price')
                upnl_d     = p.get('unrealized_pnl_dollars')
                upnl_pct   = p.get('unrealized_pnl_percent')
                stop       = p.get('stop_loss')
                tp1        = p.get('take_profit_1')
                if current is not None:
                    pnl_str  = f"  P&L: ${upnl_d:+,.0f} ({upnl_pct:+.1f}%)"
                    stop_str = f"  Stop: ${stop:.2f}" if stop else ""
                    tp_str   = f"  TP1: ${tp1:.2f}" if tp1 else ""
                    pos_lines.append(
                        f"  {sym}: {shares} shares | entry ${entry:.2f} → now ${current:.2f}{pnl_str}{stop_str}{tp_str}"
                    )
                else:
                    pos_lines.append(
                        f"  {sym}: {shares} shares @ ${entry:.2f} entry (live price unavailable)"
                    )
            pos_ctx = "\n".join(pos_lines) if pos_lines else "  (none)"
        except Exception as e:
            pos_ctx = f"  Position data unavailable ({e})."

        # Active conditionals - with live price and distance to trigger
        try:
            import yfinance as _yf
            conn = _sq.connect(str(DB_PATH))
            cond_rows = conn.execute("""
                SELECT ticker, entry_price_target, watch_tag,
                       stop_loss, take_profit_1, thesis_summary
                FROM conditional_tracking WHERE status = 'active'
                ORDER BY ticker
            """).fetchall()
            conn.close()

            # Batch-fetch all unique tickers in one yfinance call (fallback for non-streamed tickers)
            cond_tickers = list({r[0] for r in cond_rows})
            _yf_prices = {}
            if cond_tickers:
                try:
                    raw = _yf.download(
                        cond_tickers, period='1d', interval='1m',
                        group_by='ticker', progress=False, auto_adjust=True
                    )
                    for sym in cond_tickers:
                        try:
                            if len(cond_tickers) == 1:
                                _yf_prices[sym] = float(raw['Close'].iloc[-1])
                            else:
                                _yf_prices[sym] = float(raw[sym]['Close'].iloc[-1])
                        except Exception:
                            pass
                except Exception:
                    pass

            def _live_price(sym):
                # Prefer real-time WebSocket price, fall back to yfinance
                if self.realtime_enabled and self.realtime_monitor:
                    rt_price = self.realtime_monitor.get_price(sym)
                    if rt_price:
                        return rt_price
                return _yf_prices.get(sym)

            cond_lines = []
            for r in cond_rows:
                sym, target, tag, stop, tp1, thesis = r
                live = _live_price(sym)
                if live is not None:
                    dist_pct = (live - target) / target * 100
                    if (tag or '').lower() in UPSIDE_TRIGGER_TAGS:
                        arrow = "✅ ABOVE target (breakout zone)" if live >= target else f"📍 {abs(dist_pct):.1f}% below breakout"
                    else:
                        arrow = "🔴 ABOVE target" if live > target else f"📍 {abs(dist_pct):.1f}% away"
                    stop_str = f" | stop ${stop:.2f}" if stop else ""
                    tp_str   = f" | TP1 ${tp1:.2f}" if tp1 else ""
                    cond_lines.append(
                        f"  {sym} [{tag or 'untagged'}]: target ${target:.2f} | live ${live:.2f} ({arrow}){stop_str}{tp_str}"
                    )
                else:
                    cond_lines.append(
                        f"  {sym} [{tag or 'untagged'}]: target ${target:.2f} (live price unavailable)"
                    )
            cond_ctx = "\n".join(cond_lines) if cond_lines else "  none"
        except Exception as e:
            cond_ctx = f"  unavailable ({e})"

        macro_score = self._get_latest_macro_score()
        defcon = self.previous_defcon

        # ── Market snapshot - index ETFs, futures, VIX, earnings ─────────
        try:
            import yfinance as _yf2
            from datetime import date as _date

            _mkt_syms = [
                ('SPY',  'SPY (S&P 500)'),
                ('QQQ',  'QQQ (Nasdaq)'),
                ('IWM',  'IWM (Russell)'),
                ('ES=F', 'ES=F (S&P Fut)'),
                ('NQ=F', 'NQ=F (NQ Fut)'),
                ('^VIX', 'VIX'),
            ]
            mkt_lines = []
            for _sym, _lbl in _mkt_syms:
                try:
                    _fi    = _yf2.Ticker(_sym).fast_info
                    _price = _fi.get('regularMarketPrice') or _fi.get('lastPrice')
                    _prev  = _fi.get('regularMarketPreviousClose') or _fi.get('previousClose')
                    _pre   = _fi.get('preMarketPrice')
                    if _price and _prev and _prev != 0:
                        _chg = (_price - _prev) / _prev * 100
                        _line = f"  {_lbl}: ${_price:.2f} ({'+' if _chg >= 0 else ''}{_chg:.2f}%)"
                    elif _price:
                        _line = f"  {_lbl}: ${_price:.2f}"
                    else:
                        _line = f"  {_lbl}: unavailable"
                    # Pre-market price if meaningfully different
                    if _pre and _prev and _price and abs(_pre - _price) > 0.02:
                        _pre_chg = (_pre - _prev) / _prev * 100
                        _line += f"  [pre-mkt ${_pre:.2f} {'+' if _pre_chg >= 0 else ''}{_pre_chg:.2f}%]"
                    mkt_lines.append(_line)
                except Exception:
                    mkt_lines.append(f"  {_lbl}: unavailable")

            # Earnings within ±1 day for any held or watched ticker
            _watch_syms = set()
            try:
                for _p in open_positions:
                    _watch_syms.add(_p['asset_symbol'])
            except Exception:
                pass
            try:
                for _r in cond_rows:
                    _watch_syms.add(_r[0])
            except Exception:
                pass

            _today = _date.today()
            # ETFs (GLD, TLT, USO, ITA, XLE, etc.) never have earnings calendars -
            # yfinance logs a 404 ERROR for them internally before raising. Silence
            # the yfinance logger to CRITICAL during .calendar calls so those harmless
            # 404s don't pollute our logs.
            import logging as _log_mod
            _yf_log = _log_mod.getLogger('yfinance')
            _yf_log_lvl = _yf_log.level

            _earnings_flags = []
            for _sym in sorted(_watch_syms):
                try:
                    _yf_log.setLevel(_log_mod.CRITICAL)
                    _cal = _yf2.Ticker(_sym).calendar
                    _yf_log.setLevel(_yf_log_lvl)
                    if _cal is None:
                        continue
                    _dates = (_cal.get('Earnings Date', [])
                              if isinstance(_cal, dict) else list(_cal.get('Earnings Date', [])))
                    for _d in _dates:
                        try:
                            _dd = _d.date() if hasattr(_d, 'date') else _date.fromisoformat(str(_d)[:10])
                            _delta = (_dd - _today).days
                            if -1 <= _delta <= 1:
                                _tag = 'TODAY' if _delta == 0 else ('+1d' if _delta == 1 else 'YESTERDAY')
                                _earnings_flags.append(f"{_sym} ({_tag})")
                        except Exception:
                            pass
                except Exception:
                    pass
                finally:
                    _yf_log.setLevel(_yf_log_lvl)  # always restore

            _earnings_str = ', '.join(_earnings_flags) if _earnings_flags else 'none for watched/held tickers'
            mkt_lines.append(f"  Earnings ±1d: {_earnings_str}")
            market_ctx = '\n'.join(mkt_lines)
        except Exception as _me:
            market_ctx = f"  Market snapshot unavailable ({_me})"

        # ── Pull full day's DB context (same data the close briefing uses) ──
        try:
            import daily_briefing as _db_module
            day_ctx = _db_module._gather_daily_context(str(DB_PATH))
        except Exception as _dce:
            logger.warning(f"  ⚠️  Could not gather daily context: {_dce}")
            day_ctx = {}

        # ── Build time-of-day specific prompt ───────────────────────────
        from gemini_client import market_context_block as _mctx
        _live_vix = None
        try:
            _live_vix = float(mkt_lines[0].split('VIX')[1].split()[0].strip(':').strip()) if mkt_lines else None
        except Exception:
            pass
        _session_ctx = _mctx(vix=_live_vix)

        # ── Format full-day DB context for the prompt ───────────────────
        _ns   = day_ctx.get('news_summary', {})
        _mac  = day_ctx.get('macro', {})
        _dh   = day_ctx.get('defcon_history', [])
        _pro  = day_ctx.get('pro_analyses', [])
        _fl   = day_ctx.get('flash_analyses', [])
        _cong = day_ctx.get('congressional_clusters', [])
        _cls  = day_ctx.get('recent_closed', [])

        def _fmt_macro(m):
            if not m or not isinstance(m.get('yield_curve_spread'), float):
                return "  FRED data not yet available today\n"
            return (
                f"  Yield Curve (10Y-2Y): {m.get('yield_curve_spread',0):+.2f}%\n"
                f"  Fed Funds: {m.get('fed_funds_rate','N/A'):.2f}%   "
                f"Unemployment: {m.get('unemployment_rate','N/A'):.1f}%\n"
                f"  HY Credit Spreads: {m.get('hy_oas_bps','N/A'):.0f}bps   "
                f"Consumer Sentiment: {m.get('consumer_sentiment','N/A'):.1f}\n"
                f"  Macro Composite Score: {m.get('macro_score',50):.0f}/100\n"
            )

        def _fmt_defcon_timeline(dh):
            if not dh:
                return "  No monitoring cycles recorded yet today\n"
            return ''.join(
                f"  {d.get('monitoring_time','?')} - DEFCON {d.get('defcon_level','?')} "
                f"Score {d.get('signal_score',0):.1f} VIX {d.get('vix_close','?')} "
                f"Yield {d.get('bond_10yr_yield','?')}%\n"
                for d in dh
            )

        def _fmt_cong(clusters):
            if not clusters:
                return "  No significant cluster signals\n"
            lines = []
            for c in clusters[:4]:
                lines.append(
                    f"  ${c.get('ticker','?')}: {c.get('buy_count',0)} politicians "
                    f"strength={c.get('signal_strength',0):.0f} "
                    f"bipartisan={'Yes' if c.get('bipartisan') else 'No'}\n"
                )
            return ''.join(lines)

        def _fmt_pro(pro):
            if not pro:
                return "  No Pro analyses today yet\n"
            from collections import Counter
            acts = Counter(p.get('recommended_action','?') for p in pro)
            out = f"  Consensus: {dict(acts)}\n"
            for p in pro[:2]:
                out += f"  [{p.get('trigger_type','?')}] {p.get('recommended_action','?')} - {(p.get('reasoning') or '')[:200]}\n"
            return out

        def _fmt_news_history(ns, fl):
            out = (
                f"  Cycles today: {ns.get('cycles',0)}  "
                f"Avg score: {ns.get('avg_score',0):.1f}  "
                f"Peak: {ns.get('peak_score',0):.1f}  "
                f"Articles: {ns.get('total_articles',0)}\n"
                f"  Dominant type: {ns.get('dominant_crisis','N/A')}\n"
            )
            if fl:
                themes = list({f.get('dominant_theme','') for f in fl if f.get('dominant_theme')})[:4]
                if themes:
                    out += f"  Flash themes: {', '.join(themes)}\n"
            return out

        def _fmt_closed(cls):
            if not cls:
                return "  None this week\n"
            return ''.join(
                f"  {t['asset_symbol']} → {t.get('exit_reason','?')}: "
                f"${t.get('profit_loss_dollars',0):+,.2f} ({t.get('profit_loss_percent',0):+.1f}%)\n"
                for t in cls
            )

        _macro_block      = _fmt_macro(_mac)
        _defcon_timeline  = _fmt_defcon_timeline(_dh)
        _cong_block       = _fmt_cong(_cong)
        _pro_block        = _fmt_pro(_pro)
        _news_hist_block  = _fmt_news_history(_ns, _fl)
        _closed_block     = _fmt_closed(_cls)

        # ── Session-specific JSON templates ──────────────────────────────
        _MORNING_JSON = """{
  "market_regime": "risk-on | risk-off | neutral | transitioning",
  "regime_confidence": 0.0,
  "session_setup": "How today is set up at open - pre-market conditions, overnight moves, key gap analysis",
  "headline_summary": "2-3 sentence summary of what is driving markets today",
  "key_themes": ["theme1", "theme2", "theme3"],
  "biggest_risk_today": "specific near-term risk with evidence from data",
  "biggest_opportunity_today": "specific opportunity with evidence from data",
  "signal_quality_assessment": "were today's early signals meaningful or noise?",
  "macro_alignment": "how FRED macro (yield curve, credit spreads, etc.) aligns with today's setup",
  "congressional_alpha": "actionable intelligence from recent congressional trading, or 'none notable'",
  "portfolio_assessment": "position-by-position: stop/TP proximity, thesis status, risk to each",
  "position_actions": [
    {"ticker": "SYMBOL", "action": "tighten_stop | hold | take_profit | add | exit", "adjusted_stop_pct": -2.5, "adjusted_tp_pct": null, "urgency": "immediate | watch | routine", "reasoning": "one sentence why"}
  ],
  "positions_at_risk": ["TICKER: stop within X% because reason - or empty list"],
  "conditionals_to_watch": [{"ticker": "SYMBOL", "urgency": "high|medium|low", "reason": "one sentence"}],
  "defcon_forecast": "expected DEFCON level through end of session and why",
  "entry_conditions_today": "specific conditions that must be met today to trigger any new position",
  "key_levels": "critical price levels for SPY/QQQ and any held positions to monitor today",
  "first_hour_watch": "one specific setup or trigger to monitor in the first 60 minutes",
  "model_confidence": 0.0,
  "data_gaps": ["specific items that were absent or stale in today's data"]
}"""

        _MIDDAY_JSON = """{
  "market_regime": "risk-on | risk-off | neutral | transitioning",
  "regime_confidence": 0.0,
  "morning_vs_now": "how the session has tracked vs the morning setup - what changed, what held",
  "headline_summary": "2-3 sentence summary of the mid-session narrative",
  "key_themes": ["theme1", "theme2", "theme3"],
  "biggest_risk_today": "specific PM session risk with evidence from current data",
  "biggest_opportunity_today": "specific PM session opportunity with evidence from current data",
  "signal_quality_assessment": "quality and consistency of signals seen so far today",
  "macro_alignment": "how FRED macro aligns with today's intraday price action",
  "congressional_alpha": "actionable intelligence from congressional trading, or 'none notable'",
  "portfolio_assessment": "P&L update and momentum direction for each position - any thesis changes?",
  "position_actions": [
    {"ticker": "SYMBOL", "action": "tighten_stop | hold | take_profit | add | exit", "adjusted_stop_pct": -2.5, "adjusted_tp_pct": null, "urgency": "immediate | watch | routine", "reasoning": "one sentence why"}
  ],
  "positions_at_risk": ["TICKER: stop within X% because reason - or empty list"],
  "conditionals_to_watch": [{"ticker": "SYMBOL", "urgency": "high|medium|low", "reason": "one sentence"}],
  "defcon_forecast": "expected DEFCON level for the afternoon session and into close",
  "afternoon_plan": "setup heading into close - key levels, setups, risk management for each position",
  "model_confidence": 0.0,
  "data_gaps": ["specific items that were absent or stale in today's data"]
}"""

        if label == 'morning':
            session_label = "START-OF-DAY DEEP DIVE"
            session_guidance = (
                "You are HighTrade's senior pre-market strategist. Today is {now}.\n"
                "Synthesize ALL provided data into a comprehensive morning briefing. "
                "You have access to FRED macro, congressional trading signals, the full "
                "DEFCON monitoring history from overnight cycles, live market prices, "
                "open positions with stop/TP levels, and the active entry queue.\n"
                "Be specific and cite the data. No hedging, no disclaimers."
            ).format(now=now_str)
            json_template = _MORNING_JSON
        else:
            session_label = "MID-SESSION DEEP DIVE"
            session_guidance = (
                "You are HighTrade's senior midday strategist. Today is {now}.\n"
                "Synthesize ALL provided data into a comprehensive midday briefing. "
                "Focus on what has CHANGED since the morning open - regime shifts, "
                "momentum changes, conditional progress, any thesis invalidation. "
                "Use the morning_flash briefing (in INTRADAY CONTEXT) as your baseline. "
                "Be specific and cite the data. No hedging, no disclaimers."
            ).format(now=now_str)
            json_template = _MIDDAY_JSON

        prompt = f"""{session_guidance}

{_session_ctx}
══════════════════════════════════════════════════════════
SECTION 1: LIVE MARKET SNAPSHOT
══════════════════════════════════════════════════════════
{market_ctx}

══════════════════════════════════════════════════════════
SECTION 2: OPEN POSITIONS (live prices, stop/TP levels)
══════════════════════════════════════════════════════════
{pos_ctx}

══════════════════════════════════════════════════════════
SECTION 3: ENTRY QUEUE - ACTIVE CONDITIONALS (live distance to trigger)
══════════════════════════════════════════════════════════
{cond_ctx}

══════════════════════════════════════════════════════════
SECTION 4: TODAY'S NEWS INTELLIGENCE ({_ns.get('cycles', 0)} cycles)
══════════════════════════════════════════════════════════
{_news_hist_block}
Latest signal: {news_ctx}

══════════════════════════════════════════════════════════
SECTION 5: DEFCON & SIGNAL SCORE TIMELINE TODAY
══════════════════════════════════════════════════════════
{_defcon_timeline}
Current DEFCON: {defcon}/5   Macro Score: {macro_score:.0f}/100

══════════════════════════════════════════════════════════
SECTION 6: MACROECONOMIC ENVIRONMENT (FRED)
══════════════════════════════════════════════════════════
{_macro_block}
══════════════════════════════════════════════════════════
SECTION 7: CONGRESSIONAL TRADING SIGNALS
══════════════════════════════════════════════════════════
{_cong_block}
══════════════════════════════════════════════════════════
SECTION 8: GEMINI PRO ANALYSIS CONSENSUS (today)
══════════════════════════════════════════════════════════
{_pro_block}
══════════════════════════════════════════════════════════
SECTION 9: RECENT CLOSED TRADES (last 7 days)
══════════════════════════════════════════════════════════
{_closed_block}
══════════════════════════════════════════════════════════
YOUR TASK: {session_label}
══════════════════════════════════════════════════════════
Synthesize ALL of the above into a structured briefing.
Populate EVERY field. regime_confidence and model_confidence must be actual numbers 0.0-1.0.
conditionals_to_watch must only contain tickers from SECTION 3 above.
For position_actions: produce one entry per open position. adjusted_stop_pct is the stop as a percentage below current price (negative, e.g. -2.5). Use null for levels you would not change. Empty array [] if no positions.
Respond in this EXACT JSON format - no prose, no markdown, no code fences:
{json_template}"""

        # ── Call Gemini Pro with full reasoning (same tier as close briefing) ──
        from gemini_client import call as gemini_call
        logger.info(f"  🔬 {emoji} {session_label} - calling Gemini Pro (reasoning)...")
        text, in_tok, out_tok = gemini_call(
            prompt,
            model_key='reasoning',
            max_output_tokens=16000,
            caller=f'{label}_briefing',
        )

        if not text:
            raise RuntimeError(f"Gemini returned no response for {label} deep dive")

        # ── Parse full JSON response ─────────────────────────────────────
        # Strip markdown fences if present
        clean = text.strip()
        if clean.startswith('```'):
            clean = clean.split('```', 2)[1]
            if clean.startswith('json'):
                clean = clean[4:]
            clean = clean.rsplit('```', 1)[0].strip()

        result = {}
        try:
            result = json.loads(clean)
        except json.JSONDecodeError:
            # Try to extract JSON object from surrounding text
            brace = clean.find('{')
            if brace != -1:
                try:
                    result = json.loads(clean[brace:clean.rfind('}')+1])
                except Exception:
                    pass
            if not result:
                logger.warning(f"  ⚠️  Could not parse {label} briefing JSON - storing raw text")
                result = {'headline_summary': clean[:500], 'data_gaps': ['JSON parse failure']}

        # Extract key fields
        regime        = result.get('market_regime', 'unknown')
        regime_conf   = float(result.get('regime_confidence', 0.0) or 0.0)
        headline      = result.get('headline_summary', '')
        key_themes    = result.get('key_themes', [])
        biggest_risk  = result.get('biggest_risk_today', '')
        best_opp      = result.get('biggest_opportunity_today', '')
        sig_quality   = result.get('signal_quality_assessment', '')
        macro_align   = result.get('macro_alignment', '')
        cong_alpha    = result.get('congressional_alpha', '')
        port_assess   = result.get('portfolio_assessment', '')
        at_risk       = result.get('positions_at_risk', [])
        cond_watch    = result.get('conditionals_to_watch', [])
        defcon_fc_str = str(result.get('defcon_forecast', ''))
        entry_conds   = result.get('entry_conditions_today', result.get('afternoon_plan', ''))
        session_key   = result.get('session_setup', result.get('morning_vs_now', ''))
        model_conf    = float(result.get('model_confidence', 0.0) or 0.0)
        gaps_list     = result.get('data_gaps', [])
        reasoning     = result.get('reasoning_chain', session_key)

        # Extract DEFCON forecast integer
        defcon_forecast = None
        try:
            import re as _re
            m = _re.search(r'\b([1-5])\b', defcon_fc_str)
            if m:
                defcon_forecast = int(m.group(1))
        except Exception:
            pass

        logger.info(
            f"  {emoji} {session_label} ({in_tok}→{out_tok} tok): "
            f"regime={regime} DEFCON→{defcon_forecast} conf={model_conf:.2f}"
        )
        logger.info(f"  📰 {headline[:140]}...")
        if gaps_list:
            logger.info(f"  🔍 Gaps: {' | '.join(gaps_list[:5])}")

        # ── Write ALL columns to daily_briefings ────────────────────────
        date_str      = _et_now().strftime('%Y-%m-%d')
        model_key_db  = f"{label.lower().replace(' ', '_')}_flash"

        try:
            import sqlite3 as _sq
            conn = _sq.connect(str(DB_PATH))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                INSERT OR REPLACE INTO daily_briefings
                (date, model_key, model_id,
                 market_regime, regime_confidence,
                 headline_summary, key_themes_json,
                 biggest_risk, biggest_opportunity,
                 signal_quality, macro_alignment,
                 congressional_alpha, portfolio_assessment,
                 watchlist_json, entry_conditions,
                 defcon_forecast, reasoning_chain,
                 model_confidence, input_tokens, output_tokens,
                 full_response_json, data_gaps_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                date_str, model_key_db, 'gemini-2.5-pro',
                regime, regime_conf,
                headline, json.dumps(key_themes),
                biggest_risk, best_opp,
                sig_quality, macro_align,
                cong_alpha, port_assess,
                json.dumps(cond_watch), entry_conds,
                defcon_fc_str, reasoning,
                model_conf, in_tok, out_tok,
                json.dumps(result),
                json.dumps(gaps_list) if gaps_list else None,
            ))
            conn.commit()
            conn.close()
            logger.info(f"  💾 {emoji} {session_label} saved to daily_briefings ({model_key_db})")
        except Exception as db_err:
            logger.warning(f"  ⚠️  Briefing DB write failed: {db_err}")

        # ── Send rich Slack alert (same format as close briefing) ────────
        notify_payload = {
            'model_key':        model_key_db,
            'market_regime':    f"{emoji} {regime.title()}",
            'headline':         headline,
            'biggest_risk':     biggest_risk,
            'best_opportunity': best_opp,
            'defcon_forecast':  (
                defcon_fc_str[:120] if defcon_fc_str
                else f"DEFCON {defcon}/5 (no change forecast)"
            ),
            'data_gaps':        gaps_list,
            'in_tokens':        in_tok,
            'out_tokens':       out_tok,
        }
        # Prepend session label so Slack header is clear
        notify_payload['market_regime'] = f"{emoji} *{session_label}* - {regime.title()}"
        self.alerts.send_notify('daily_briefing', notify_payload)
        self.alerts.send_silent_log('daily_briefing', notify_payload)

        # Store latest flash DEFCON forecast for use by next monitoring cycle
        self._last_flash_forecast = defcon_forecast
        if defcon_forecast:
            logger.info(f"  🧭 Flash DEFCON forecast stored: {defcon_forecast}/5")

        # Apply position_actions from flash briefing (tighten stops, adjust TPs)
        try:
            adjusted = self.broker.decision_engine.apply_briefing_position_actions()
            if adjusted > 0:
                logger.info(f"  🔧 Briefing position actions: {adjusted} stop/TP adjustment(s) applied")
        except Exception as _bpa_e:
            logger.warning(f"  ⚠️  Briefing position actions failed: {_bpa_e}")

        # Update per-ticker attention scores from conditionals_to_watch list
        # (convert to the format _update_attention_scores expects)
        watch_list = [
            {'ticker': w.get('ticker', ''), 'urgency': w.get('urgency', 'low'), 'reason': w.get('reason', '')}
            for w in cond_watch if w.get('ticker')
        ]
        self._update_attention_scores(watch_list)

    def _update_attention_scores(self, watch_list: list):
        """Decay all active conditional attention scores -5/cycle, apply flash bumps + price proximity."""
        import sqlite3 as _sq
        urgency_bump = {'high': 25, 'medium': 15, 'low': 8}
        now = datetime.now().isoformat()
        try:
            conn = _sq.connect(str(DB_PATH))

            # 1. Decay all active conditionals by 5 (floor 0)
            conn.execute("""
                UPDATE conditional_tracking
                SET attention_score      = MAX(0, COALESCE(attention_score, 0) - 5),
                    attention_updated_at = ?
                WHERE status = 'active'
            """, (now,))

            # 2. Flash mention bumps
            for item in (watch_list or []):
                ticker = (item.get('ticker') or '').upper().strip()
                bump   = urgency_bump.get(str(item.get('urgency', 'low')).lower(), 8)
                if ticker:
                    conn.execute("""
                        UPDATE conditional_tracking
                        SET attention_score      = MIN(100, COALESCE(attention_score, 0) + ?),
                            attention_updated_at = ?
                        WHERE ticker = ? AND status = 'active'
                    """, (bump, now, ticker))
                    logger.info(f"  📍 Attention bump: {ticker} +{bump} (urgency={item.get('urgency','low')})")

            # 3. Price proximity bump (+15 if within 2% of entry target)
            rows = conn.execute("""
                SELECT id, ticker, entry_price_target
                FROM conditional_tracking
                WHERE status = 'active' AND entry_price_target IS NOT NULL
            """).fetchall()
            conn.commit()

            prox_bumped = []
            for row_id, ticker, target in rows:
                try:
                    # Prefer real-time WebSocket price, fall back to yfinance
                    price = None
                    if self.realtime_enabled and self.realtime_monitor:
                        price = self.realtime_monitor.get_price(ticker)
                    if not price:
                        import yfinance as _yf
                        price = _yf.Ticker(ticker).fast_info.get('lastPrice') or \
                                _yf.Ticker(ticker).fast_info.get('regularMarketPrice')
                    if price and target and abs(float(price) - float(target)) / float(target) <= 0.02:
                        conn.execute("""
                            UPDATE conditional_tracking
                            SET attention_score      = MIN(100, COALESCE(attention_score, 0) + 15),
                                attention_updated_at = ?
                            WHERE id = ?
                        """, (now, row_id))
                        prox_bumped.append(ticker)
                except Exception:
                    pass

            conn.commit()
            conn.close()
            if prox_bumped:
                logger.info(f"  📍 Proximity bump (+15) applied to: {', '.join(prox_bumped)}")
        except Exception as e:
            logger.warning(f"  ⚠️  _update_attention_scores failed: {e}")

    def _check_acquisition_pipeline(self, force: bool = False):
        """
        Check and trigger the acquisition pipeline at specific checkpoints:
        1. Pre-market: 9:00 AM (Ready before open)
        2. Mid-day: 12:30 PM (Mid-day adjustment)
        3. Close: Handled by _check_daily_briefing
        """
        try:
            now = _et_now()                    # Eastern Time for market schedule
            today = now.strftime('%Y-%m-%d')

            # Define checkpoints (all ET)
            checkpoints = [
                (9, 0, 'pre_market'),   # 9:00 AM ET
                (12, 30, 'mid_day')     # 12:30 PM ET
            ]

            for hour, minute, label in checkpoints:
                # Check if it's past the checkpoint and we haven't run it today
                past_time = (now.hour > hour or (now.hour == hour and now.minute >= minute))
                run_key = f"{label}_{today}"

                if (force or past_time) and run_key not in self._pipeline_runs:
                    logger.info(f"🔬 Triggering {label.replace('_', ' ')} acquisition analysis...")
                    self._pipeline_runs.add(run_key)

                    # Researcher -> Analyst chain for any pending items (from Hound etc)
                    # Note: verifier runs independently on its own cycle - no duplicate call here
                    self._run_acquisition_pipeline(today, skip_date_check=True)

        except Exception as e:
            logger.warning(f"Pipeline checkpoint check failed: {e}")

    def _check_pending_acquisition_work(self):
        """
        Opportunistically drain newly queued acquisition work between fixed checkpoints.

        Why: Hound, daily briefing, or exit-review jobs can enqueue new `pending`
        watchlist rows after the 12:30 PM checkpoint. Without this, they sit idle
        until the close briefing pipeline runs.

        Guardrails:
          - throttled to once every 15 minutes
          - only fires when there are pending watchlist rows or actionable research rows
        """
        try:
            now = _et_now()
            if self._last_pending_pipeline_check:
                elapsed = (now - self._last_pending_pipeline_check).total_seconds()
                if elapsed < 15 * 60:
                    return

            import sqlite3 as _sq
            conn = _sq.connect(str(DB_PATH))
            pending_watchlist = conn.execute(
                "SELECT COUNT(*) FROM acquisition_watchlist WHERE status='pending'"
            ).fetchone()[0]
            ready_research = conn.execute(
                "SELECT COUNT(*) FROM stock_research_library WHERE status IN ('library_ready', 'partial')"
            ).fetchone()[0]
            conn.close()

            self._last_pending_pipeline_check = now

            if pending_watchlist <= 0 and ready_research <= 0:
                return

            logger.info(
                "🔁 Pending acquisition work detected - draining queue now "
                f"(watchlist={pending_watchlist}, ready={ready_research})"
            )
            self._run_acquisition_pipeline(now.strftime('%Y-%m-%d'), skip_date_check=True)

        except Exception as e:
            logger.warning(f"Pending acquisition work check failed: {e}")

    def _check_daily_briefing(self, force: bool = False):
        """Fire daily briefing once per day after market close (4:30 PM ET)."""
        try:
            now = _et_now()                    # Eastern Time - market close is 4 PM ET
            today = now.strftime('%Y-%m-%d')
            market_close_hour = 16  # 4 PM ET - briefing triggers at 4:30 ET
            market_close_minute = 30

            # Only fire after 4:30 PM and only once per date
            after_close = (now.hour > market_close_hour or
                           (now.hour == market_close_hour and now.minute >= market_close_minute))

            if not force and (not after_close or self._daily_briefing_date == today):
                return

            # DB guard: survive orchestrator restarts - don't re-fire if reasoning briefing already in DB today
            if not force and self._daily_briefing_date != today:
                try:
                    import sqlite3 as _sq2
                    _c = _sq2.connect(str(DB_PATH))
                    _hit = _c.execute(
                        "SELECT 1 FROM daily_briefings WHERE date=? AND model_key='reasoning' LIMIT 1",
                        (today,)
                    ).fetchone()
                    _c.close()
                    if _hit:
                        self._daily_briefing_date = today  # stamp in-memory
                        logger.debug(f"  ⏭️  Daily briefing already in DB for {today} - skipping")
                        return
                except Exception:
                    pass  # If DB check fails, proceed normally

            logger.info("📋 Triggering daily market briefing (Gemini 3 Pro, deep reasoning)...")
            self._daily_briefing_date = today

            from daily_briefing import run_daily_briefing
            results = run_daily_briefing(compare_models=False)  # production: reasoning tier only

            # Log model summary
            for model_key, r in results.items():
                if 'error' not in r:
                    logger.info(
                        f"  📋 {model_key}: {r.get('market_regime','?')} | "
                        f"confidence={r.get('model_confidence',0):.2f} | "
                        f"{r.get('_input_tokens',0)}→{r.get('_output_tokens',0)} tokens"
                    )

            # Apply position_actions from close briefing
            try:
                adjusted = self.broker.decision_engine.apply_briefing_position_actions()
                if adjusted > 0:
                    logger.info(f"  🔧 Close briefing position actions: {adjusted} adjustment(s) applied")
            except Exception as _bpa_e:
                logger.warning(f"  ⚠️  Close briefing position actions failed: {_bpa_e}")

            # Trigger acquisition pipeline after briefing (researcher then analyst)
            # This handles the "at close" requirement.
            self._run_acquisition_pipeline(today)

        except Exception as e:
            logger.warning(f"Daily briefing failed: {e}")

    def _run_acquisition_pipeline(self, date_str: str, skip_date_check: bool = False):
        """
        Run the acquisition research → analyst pipeline.

        skip_date_check: if True, runs even if it already ran today (for intraday checkpoints).
        """
        if not skip_date_check and self._acquisition_pipeline_date == date_str:
            return  # Already ran full daily pipeline

        logger.info("🔬 Running acquisition pipeline: researcher → analyst...")
        if not skip_date_check:
            self._acquisition_pipeline_date = date_str

        import time as _time

        # Step 1: Researcher - pick up any pending items
        researched = []
        try:
            from acquisition_researcher import run_research_cycle
            researched = run_research_cycle()
            if researched:
                logger.info(f"  📚 Researcher: {len(researched)} new tickers → {researched}")
            else:
                logger.info("  📚 Researcher: no new pending items")
        except Exception as e:
            logger.error(f"  ❌ Acquisition researcher failed: {e}")
            # Continue - analyst may still have library_ready items from a prior run

        # Brief pause if researcher just did work (don't slam Gemini)
        if researched:
            _time.sleep(10)

        # Step 2: Analyst - ALWAYS run to catch any library_ready items waiting
        # (items from prior research runs, hound auto-promotes, manual adds, etc.)
        try:
            from acquisition_analyst import run_analyst_cycle
            # Generate sector context for analyst
            _sector_ctx2 = ''
            try:
                _sc2 = self.sector_analyzer.get_sector_context(
                    crisis_type='market_correction',  # generic fallback for pipeline runs
                    defcon_level=self.monitor.defcon_level,
                    is_winding_down=getattr(self.monitor, 'is_winding_down', False),
                    deescalation_score=getattr(self, '_last_deesc_score', 0),
                )
                _sector_ctx2 = _sc2.get('rotation_guidance', '')
            except Exception:
                pass

            results = run_analyst_cycle(extra_context={
                'defcon_level': self.monitor.defcon_level,
                'news_score':   getattr(self, '_last_news_score', 0),
                'is_winding_down': getattr(self.monitor, 'is_winding_down', False),
                'deescalation_score': getattr(self, '_last_deesc_score', 0),
                'sector_guidance': _sector_ctx2,
            })
            if results:
                promoted = [
                    r.get('_ticker') for r in results
                    if r.get('should_enter') and r.get('research_confidence', 0) >= 0.7
                ]
                logger.info(
                    f"  🧠 Analyst: {len(results)} analyzed, "
                    f"{len(promoted)} conditionals set → {promoted}"
                )
            else:
                logger.info("  🧠 Analyst: no library_ready items to analyze")
        except Exception as e:
            logger.error(f"  ❌ Acquisition analyst failed: {e}")

        logger.info("✅ Acquisition pipeline complete")

        # Step 3: Re-queue any analyst_pass items for daily reanalysis
        try:
            requeued = self._requeue_analyst_pass_items(date_str)
            if requeued > 0:
                logger.info(f"  🔄 Requeued {requeued} analyst_pass tickers for reanalysis tomorrow")
        except Exception as e:
            logger.error(f"  ❌ analyst_pass requeue failed: {e}")

    def _requeue_analyst_pass_items(self, today: str) -> int:
        """
        After each daily pipeline run, re-queue tickers that previously got analyst_pass
        so they're re-evaluated with fresh data. After 3 failed re-evaluations, archive them.

        Returns the number of tickers re-queued.
        """
        import sqlite3 as _sq
        requeued = archived = 0

        try:
            conn = _sq.connect(str(DB_PATH))
            conn.row_factory = _sq.Row

            # Find tickers with analyst_pass that have no row already queued for today
            candidates = conn.execute("""
                SELECT ticker, source, market_regime, biggest_risk, biggest_opportunity,
                       COUNT(*) AS pass_count
                FROM acquisition_watchlist
                WHERE status = 'analyst_pass'
                  AND date_added < ?
                GROUP BY ticker
                HAVING NOT EXISTS (
                    SELECT 1 FROM acquisition_watchlist a2
                    WHERE a2.ticker = acquisition_watchlist.ticker
                      AND a2.date_added = ?
                )
            """, (today, today)).fetchall()

            for row in candidates:
                if row['pass_count'] >= 3:
                    # Archive after 3 failed re-evaluations - stop cycling
                    conn.execute("""
                        UPDATE acquisition_watchlist SET status = 'archived'
                        WHERE ticker = ? AND status = 'analyst_pass'
                    """, (row['ticker'],))
                    logger.info(
                        f"  📦 {row['ticker']} archived after {row['pass_count']} analyst_pass cycles"
                    )
                    archived += 1
                else:
                    attempt = row['pass_count'] + 1
                    conn.execute("""
                        INSERT OR REPLACE INTO acquisition_watchlist
                          (date_added, ticker, source, market_regime, model_confidence,
                           entry_conditions, biggest_risk, biggest_opportunity, status, notes)
                        VALUES (?, ?, ?, ?, 0.5,
                                'Reanalysis #' || ? || ' - conditions may have changed',
                                ?, ?, 'pending', 'reanalysis')
                    """, (
                        today, row['ticker'], row['source'], row['market_regime'],
                        attempt, row['biggest_risk'], row['biggest_opportunity']
                    ))
                    logger.info(
                        f"  🔁 {row['ticker']} requeued (attempt #{attempt}) for fresh analyst pass"
                    )
                    requeued += 1

            conn.commit()
            conn.close()

        except Exception as e:
            logger.error(f"_requeue_analyst_pass_items error: {e}")

        if archived:
            logger.info(f"  📦 Archived {archived} stale analyst_pass tickers (≥3 attempts)")

        return requeued

    def update_dashboard(self):
        """Generate updated dashboard"""
        try:
            logger.info("Updating dashboard...")
            generate_dashboard_html()
            logger.info("✅ Dashboard updated")
        except Exception as e:
            logger.warning(f"Dashboard update failed: {e}")

    def print_status_summary(self):
        """Print current system status"""
        logger.info("\n" + "="*60)
        logger.info("SYSTEM STATUS SUMMARY")
        logger.info("="*60)
        logger.info(f"Monitoring Cycles: {self.monitoring_cycles}")
        logger.info(f"Alerts Sent: {self.alerts_sent}")
        logger.info(f"Log File: {LOG_FILE}")

        # Real-time stream status
        if self.realtime_enabled and self.realtime_monitor:
            st = self.realtime_monitor.get_status()
            logger.info(f"🔴 Stream: {st['status']} | {st['ticks_received']} ticks | "
                        f"{st['subscribed_tickers']} tickers | "
                        f"{st['entry_triggers']} entries | {st['exit_triggers']} exits")

        # Get latest monitoring data
        try:
            self.monitor.connect()
            status = self.monitor.get_status()
            if status:
                logger.info(f"Last Status: DEFCON {status['defcon_level']}")
                logger.info(f"Signal Score: {status['signal_score']:.1f}")
            self.monitor.disconnect()
        except:
            pass

        logger.info("="*60)

    def _wait_for_db(self, timeout_seconds: int = 120):
        """Ensure the SQLite DB is available before proceeding. Wait up to timeout_seconds."""
        import time as _time
        import sqlite3 as _sq
        start = _time.time()
        while True:
            try:
                conn = _sq.connect(str(DB_PATH), timeout=5)
                conn.execute('SELECT 1')
                conn.close()
                logger.info("✅ Database available")
                return True
            except Exception as _e:
                logger.warning(f"Database not available yet: {_e}")
                if _time.time() - start > timeout_seconds:
                    logger.error("Database did not become available within timeout")
                    return False
                _time.sleep(5)

    def run_continuous(self, interval_minutes=15):
        """Run system continuously with slash command support"""
        logger.info(f"\n🚀 Starting HighTrade in continuous mode")
        # Ensure DB is reachable before starting scheduled briefings and pipelines
        if not self._wait_for_db(timeout_seconds=180):
            # If DB is unavailable, alert and continue but avoid firing time-sensitive jobs
            self.alerts.send_slack(
                "⚠️ HighTrade startup: database unavailable. Time-sensitive briefings will be paused until DB recovers.",
                defcon_level=2
            )
        logger.info(f"   Interval: {interval_minutes} minutes")
        logger.info(f"   Log: {LOG_FILE}")
        logger.info(f"   Commands: python3 hightrade_cmd.py /help")

        # Start real-time WebSocket stream (market-hours price monitoring)
        if self.realtime_enabled and self.broker_mode != 'disabled':
            try:
                self.realtime_monitor.start()
                logger.info("🔴 Real-time price stream started")
            except Exception as e:
                logger.warning(f"⚠️  Real-time stream start failed: {e}")

        cycle = 0
        try:
            while True:
                # Check for commands before each cycle
                self.cmd_processor.check_for_commands()

                # Respect stop commands
                if self.cmd_processor.should_stop:
                    logger.info("🛑 Stop command received - shutting down")
                    break

                cycle += 1

                # Run monitoring (always runs, even on hold)
                self.run_monitoring_cycle()

                # Update dashboard every 3 cycles
                if cycle % 3 == 0:
                    self.update_dashboard()

                # Pick up interval changes
                if self._new_interval is not None:
                    interval_minutes = self._new_interval
                    self._new_interval = None
                    logger.info(f"🔧 Interval changed to {interval_minutes} minutes")

                logger.info(f"\n⏳ Next cycle in {interval_minutes} minutes...")

                # Sleep in short increments so we can check for commands
                sleep_seconds = interval_minutes * 60
                check_interval = 2  # Check for commands every 2 seconds
                elapsed = 0
                while elapsed < sleep_seconds:
                    time.sleep(check_interval)
                    elapsed += check_interval

                    # Check for commands during sleep
                    cmds = self.cmd_processor.check_for_commands()

                    # If /update was issued, break out of sleep to run cycle now
                    for c in cmds:
                        if c['command'] == '/update':
                            elapsed = sleep_seconds  # Break sleep
                            break

                    # If stop requested, break immediately
                    if self.cmd_processor.should_stop:
                        break

        except KeyboardInterrupt:
            logger.info("\n\n✓ System stopped by user")
            self.print_status_summary()
        except Exception as e:
            logger.error(f"Fatal error: {e}")
            self.print_status_summary()
            sys.exit(1)

        # Final shutdown
        if self.realtime_enabled and self.realtime_monitor:
            try:
                self.realtime_monitor.stop()
            except Exception:
                pass
        self.alerts.send_slack(
            "🛑 HighTrade bot has shut down.",
            defcon_level=self.previous_defcon
        )
        self.print_status_summary()

    def run_test(self):
        """Run single test cycle"""
        logger.info("🧪 Running test cycle...")
        self.run_monitoring_cycle()
        self.update_dashboard()
        self.print_status_summary()

    def _get_latest_macro_score(self) -> float:
        """Return the most recent macro_score from DB (used for pre-purchase gate live_state)."""
        conn = None
        try:
            from trading_db import get_sqlite_conn
            conn = get_sqlite_conn(str(DB_PATH))
            cursor = conn.cursor()
            cursor.execute("""
                SELECT macro_score FROM macro_indicators
                ORDER BY created_at DESC LIMIT 1
            """)
            row = cursor.fetchone()
            return float(row[0]) if row else 50.0
        except Exception:
            return 50.0  # neutral fallback - don't block gate on DB error
        finally:
            if conn:
                conn.close()

    def _check_breaking_news_in_db(self):
        """Check database for recent breaking news signals (within last 4 hours)"""
        conn = None
        try:
            import sqlite3
            from datetime import datetime, timedelta

            logger.info("  🔍 Checking database for breaking news...")
            from trading_db import get_sqlite_conn
            conn = get_sqlite_conn(str(DB_PATH))
            cursor = conn.cursor()

            # Check for breaking news from last 4 hours
            cutoff_time = (datetime.now() - timedelta(hours=4)).isoformat()
            logger.info(f"     Cutoff time: {cutoff_time}")

            cursor.execute("""
                SELECT news_signal_id, news_score, dominant_crisis_type,
                       crisis_description, recommended_defcon, article_count,
                       breaking_count, avg_confidence, sentiment_summary,
                       articles_json, timestamp
                FROM news_signals
                WHERE breaking_news_override = 1
                AND timestamp > ?
                ORDER BY timestamp DESC
                LIMIT 1
            """, (cutoff_time,))

            row = cursor.fetchone()

            if row:
                logger.warning(f"  🔥 ACTIVE BREAKING NEWS from database (ID: {row[0]})")
                logger.warning(f"     {row[3]}")
                return {
                    'news_signal_id': row[0],
                    'news_score': row[1],
                    'dominant_crisis_type': row[2],
                    'crisis_description': row[3],
                    'recommended_defcon': row[4],
                    'article_count': row[5],
                    'breaking_count': row[6],
                    'avg_confidence': row[7],
                    'sentiment_summary': row[8],
                    'contributing_articles': json.loads(row[9]) if row[9] else [],
                    'breaking_news_override': True,
                    'timestamp': row[10]
                }
            else:
                logger.info("     No breaking news found")
            return None

        except Exception as e:
            logger.error(f"Failed to check breaking news in database: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
        finally:
            if conn:
                conn.close()

    def _record_news_signal(self, news_signal, articles_full=None, gemini_flash=None):
        """Store news signal in database with full rich data for LLM access"""
        conn = None
        try:
            from trading_db import get_sqlite_conn
            conn = get_sqlite_conn(str(DB_PATH))
            cursor = conn.cursor()

            # Serialize all articles with full description (not just top 5)
            articles_full_json = None
            if articles_full:
                articles_full_json = json.dumps([
                    {
                        'title': a.title,
                        'description': a.description[:400] if a.description else '',
                        'source': a.source,
                        'published_at': a.published_at.isoformat(),
                        'url': a.url,
                        'relevance_score': a.relevance_score
                    }
                    for a in articles_full
                ])

            cursor.execute("""
                INSERT INTO news_signals
                (news_score, dominant_crisis_type, crisis_description,
                 breaking_news_override, recommended_defcon, article_count,
                 breaking_count, avg_confidence, sentiment_summary, articles_json,
                 sentiment_net_score, signal_concentration, crisis_distribution_json,
                 score_components_json, keyword_hits_json, articles_full_json,
                 gemini_flash_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                news_signal['news_score'],
                news_signal['dominant_crisis_type'],
                news_signal['crisis_description'],
                news_signal['breaking_news_override'],
                news_signal.get('recommended_defcon'),
                news_signal['article_count'],
                news_signal['breaking_count'],
                news_signal['avg_confidence'],
                news_signal['sentiment_summary'],
                json.dumps(news_signal['contributing_articles'][:5]),  # legacy top-5
                news_signal.get('sentiment_net_score', 50.0),
                news_signal.get('signal_concentration', 0.0),
                json.dumps(news_signal.get('crisis_distribution', {})),
                json.dumps(news_signal.get('score_components', {})),
                json.dumps(news_signal.get('keyword_hits', {})),
                articles_full_json,
                json.dumps(gemini_flash) if gemini_flash else None
            ))

            conn.commit()
            signal_id = cursor.lastrowid
            logger.debug(f"News signal recorded to database (ID={signal_id})")
            return signal_id

        except Exception as e:
            logger.error(f"Failed to record news signal: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
        finally:
            if conn:
                conn.close()

    def _detect_new_news(self, fresh_news_signal: dict) -> tuple:
        """
        Calculate how many articles are genuinely new (not in previous signal).

        Compares current articles against the last recorded news signal to identify
        new content based on article URLs.

        Args:
            fresh_news_signal: Current news signal dict with contributing_articles

        Returns:
            Tuple of (new_article_count: int, latest_articles: list)
            - new_article_count: Number of articles not in previous signal
            - latest_articles: Articles sorted by publish time (newest first)
        """
        conn = None
        try:
            from trading_db import get_sqlite_conn
            from datetime import datetime, timedelta

            conn = get_sqlite_conn(str(DB_PATH))
            cursor = conn.cursor()

            # Get timestamp and ALL articles from last news signal
            # Use articles_full_json (full article list), fall back to articles_json (legacy top-5)
            cursor.execute("""
                SELECT timestamp, articles_full_json, articles_json
                FROM news_signals
                ORDER BY timestamp DESC
                LIMIT 1
            """)

            last_signal = cursor.fetchone()

            # If this is the first news signal ever, all articles are "new"
            if not last_signal:
                logger.info("  🆕 First news signal ever")
                return (fresh_news_signal['article_count'],
                        fresh_news_signal['contributing_articles'])

            # Calculate time since last signal
            last_timestamp = datetime.fromisoformat(last_signal[0])
            time_since_last = (datetime.now() - last_timestamp).total_seconds() / 60

            current_articles = fresh_news_signal['contributing_articles']

            # If last signal was > 60 minutes ago, consider news potentially new
            if time_since_last > 60:
                logger.info(f"  ⏰ Last signal was {time_since_last:.0f} min ago - checking for new articles")

            # Prefer full article list; fall back to legacy top-5
            last_articles_raw = last_signal[1] or last_signal[2]
            last_articles_json = json.loads(last_articles_raw) if last_articles_raw else []
            last_article_urls = {a.get('url') for a in last_articles_json if a.get('url')}

            # Find truly new articles (not in last signal)
            new_articles = [
                a for a in current_articles
                if a.get('url') and a.get('url') not in last_article_urls
            ]

            new_count = len(new_articles)

            # Log the news status
            logger.info(f"  📊 News status: {len(current_articles)} total articles, {new_count} new since last signal")

            # Sort articles by publish time (newest first) for display
            articles_to_show = sorted(
                current_articles,
                key=lambda x: x.get('published_at', ''),
                reverse=True
            )

            return (new_count, articles_to_show)

        except Exception as e:
            logger.error(f"Error analyzing news articles: {e}")
            import traceback
            logger.error(traceback.format_exc())
            # On error, return all articles as potentially new
            return (fresh_news_signal['article_count'],
                    fresh_news_signal['contributing_articles'])
        finally:
            if conn:
                conn.close()

    def monitor_and_exit_positions(self):
        """Monitor all open positions and detect exit conditions"""
        # Reset each cycle - exits are re-detected fresh from live prices every run,
        # so accumulating them just inflates the pending count incorrectly.
        self.pending_trade_exits = []

        exit_recommendations = self.paper_trading.monitor_all_positions()

        if exit_recommendations:
            logger.info("\n" + "="*60)
            logger.info("⚠️  EXIT SIGNALS DETECTED")
            logger.info("="*60)

            for exit_rec in exit_recommendations:
                logger.info(f"{exit_rec['message']}")
                self.pending_trade_exits.append(exit_rec)

            logger.info("="*60 + "\n")

        # ── No-exit-framework check ───────────────────────────────────────
        # Any open position with no stop_loss AND no take_profit_1 has no exit
        # plan. Flag it and queue it for analyst review so the analyst sets
        # proper exit levels - same pipeline as a conditional entry, but for exits.
        self._check_positions_missing_exit_framework()

    def _check_positions_missing_exit_framework(self):
        """
        Scan open positions for missing stop/TP levels and run the dedicated
        exit_analyst directly - bypasses the acquisition pipeline which is
        designed for entry decisions, not exit frameworks.

        exit_analyst.py handles its own 20-hour guard, Gemini call, DB write,
        and Slack alert. This method just wires it into the monitoring cycle
        with the current macro context.
        """
        try:
            macro_score = self._get_latest_macro_score() or 50.0
            current_defcon = getattr(self, '_last_defcon', 5)
            processed = exit_analyst.run_exit_analysis(
                defcon=current_defcon,
                macro_score=macro_score,
                alerts=self.alerts,
            )
            if processed:
                logger.info(f"  🎯 Exit frameworks set for: {processed}")
        except Exception as e:
            logger.warning(f"  _check_positions_missing_exit_framework failed: {e}")

    def execute_pending_trades(self, auto_approve=False):
        """
        Execute pending trade alerts

        auto_approve: If True, automatically approve all pending trades
                      If False, require manual approval for each
        """
        if not self.pending_trade_alerts:
            logger.info("No pending trade alerts")
            return []

        logger.info("Processing pending trade alerts: %d item(s)", len(self.pending_trade_alerts))
        executed = []
        for idx, p in enumerate(list(self.pending_trade_alerts)):
            try:
                logger.info(f"  ▶ Pending[{idx}]: {p.get('ticker')} (conditional_id={p.get('conditional_id')})")
                # Basic validation
                missing = [k for k in ('ticker','side','shares','order_type') if k not in p]
                if missing:
                    logger.warning(f"  ❌ Skipping pending[{idx}] - missing required fields: {missing}")
                    continue
                # Ensure account/paper vs live
                account = p.get('account','paper')
                if account != 'paper' and not getattr(self, 'allow_live_orders', False):
                    logger.warning(f"  ❌ Skipping pending[{idx}] - live orders not allowed in this run (account={account})")
                    continue
                # Attempt to place paper order via broker's paper_trading interface
                # Broker's paper trading engine lives under broker.decision_engine.paper_trading
                pt = None
                try:
                    pt = self.broker.decision_engine.paper_trading
                except Exception:
                    pt = None
                if pt:
                    try:
                        # Use paper_trading.manual_buy API which handles DB writes and mirror-to-broker
                        if hasattr(pt, 'manual_buy'):
                            res = pt.manual_buy(p['ticker'], int(p.get('shares') or p.get('qty') or 0), price_override=p.get('limit_price'))
                            if res.get('ok'):
                                logger.info(f"  ✅ Placed paper manual_buy for {p['ticker']}: trade_id={res.get('trade_id')}")
                                executed.append(p.get('conditional_id') or p.get('ticker'))
                            else:
                                logger.warning(f"  ⚠️  paper_trading.manual_buy failed for {p['ticker']}: {res.get('message')}")
                        else:
                            # Fallback: try Alpaca-like place_order on underlying broker shim
                            if hasattr(pt, 'alpaca') and hasattr(pt.alpaca, 'place_order'):
                                qty = int(p.get('shares') or p.get('qty') or 0)
                                res = pt.alpaca.place_order(p['ticker'], qty, p.get('side','buy'))
                                if res.get('ok'):
                                    logger.info(f"  ✅ Placed broker order for {p['ticker']}: {res.get('order',{}).get('id','?')}")
                                    executed.append(p.get('conditional_id') or p.get('ticker'))
                                else:
                                    logger.warning(f"  ⚠️  Broker place_order failed for {p['ticker']}: {res.get('error')}")
                            else:
                                logger.warning("  ⚠️  No known paper order method available on paper_trading")
                    except Exception as e:
                        logger.warning(f"  ⚠️  Failed to place paper order for pending[{idx}]: {e}")
                else:
                    logger.warning("  ⚠️  Broker has no paper_trading interface; cannot place paper orders")
            except Exception as e:
                logger.exception(f"Error processing pending[{idx}]: {e}")
        # Clear pending after processing
        self.pending_trade_alerts = []
        return executed

    def execute_pending_exits(self, auto_exit=True):
        """
        Execute pending exit signals

        auto_exit: If True, automatically exit all positions that hit targets/stops
                   If False, require manual approval for each
        """
        if not self.pending_trade_exits:
            return []

        exited_trades = []

        for exit_rec in self.pending_trade_exits:
            logger.info(f"\n📋 Exiting position:")
            logger.info(f"   Trade ID: {exit_rec['trade_id']}")
            logger.info(f"   Asset: {exit_rec['asset_symbol']}")
            logger.info(f"   Reason: {exit_rec['reason']}")
            logger.info(f"   P&L: {exit_rec['profit_loss_pct']:+.2f}%")

            if auto_exit:
                logger.info("   ✅ Auto-exiting")
                should_exit = True
            else:
                response = input("   Exit position? (y/n): ").strip().lower()
                should_exit = response == 'y'

            if should_exit:
                # Normalize exit reasons to valid set: profit_target, stop_loss, manual, invalidation
                _exit_reason_map = {
                    'profit_target': 'profit_target',
                    'stop_loss': 'stop_loss',
                    'trailing_stop': 'stop_loss',
                    'time_limit': 'manual',
                    'time_and_loss': 'manual',
                    'defcon_revert': 'manual',
                    'manual': 'manual',
                    'invalidation': 'invalidation',
                }
                normalized_reason = _exit_reason_map.get(exit_rec['reason'], 'manual')
                success = self.paper_trading.exit_position(
                    exit_rec['trade_id'],
                    normalized_reason,
                    exit_rec['exit_price']
                )
                if success:
                    exited_trades.append(exit_rec['trade_id'])
                    logger.info(f"   ✅ EXITED")
            else:
                logger.info("   ❌ Skipped by user")

        self.pending_trade_exits = []
        return exited_trades

    def print_portfolio_status(self):
        """Print current portfolio status"""
        perf = self.paper_trading.get_portfolio_performance()
        open_pos = self.paper_trading.get_open_positions()

        logger.info("\n" + "="*60)
        logger.info("📊 PORTFOLIO STATUS")
        logger.info("="*60)

        logger.info(f"Total Trades: {perf['total_trades']}")
        logger.info(f"  Open: {perf['open_trades']}")
        logger.info(f"  Closed: {perf['closed_trades']}")
        logger.info(f"  Winners: {perf.get('winning_trades', 0)}")
        logger.info(f"  Losers: {perf.get('losing_trades', 0)}")

        if perf['closed_trades'] > 0:
            logger.info(f"\nPerformance:")
            logger.info(f"  Total P&L: ${perf['total_profit_loss_dollars']:+,.0f} "
                       f"({perf['total_profit_loss_percent']:+.2f}%)")
            logger.info(f"  Win Rate: {perf['win_rate']:.1f}%")
            logger.info(f"  Profit Factor: {perf['profit_factor']:.2f}")

        if open_pos:
            logger.info(f"\nOpen Positions: {len(open_pos)}")
            for pos in open_pos:
                logger.info(f"  • {pos['asset_symbol']}: {pos['shares']} shares @ ${pos['entry_price']:.2f}")

        if perf.get('by_asset'):
            logger.info(f"\nPerformance by Asset:")
            for asset, metrics in perf['by_asset'].items():
                logger.info(f"  {asset}: {metrics['trades']} trades, "
                           f"${metrics['total_pnl']:+,.0f}, {metrics['win_rate']:.0f}% win rate")

        logger.info("="*60 + "\n")


def main():
    """Main entry point"""
    import argparse

    parser = argparse.ArgumentParser(
        description='HighTrade Orchestrator - Trading Bot System Controller',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run continuous monitoring (15-minute intervals, manual mode)
  python3 hightrade_orchestrator.py continuous

  # Run with autonomous broker (semi-auto mode)
  python3 hightrade_orchestrator.py continuous --broker semi_auto

  # Run with full autonomous broker
  python3 hightrade_orchestrator.py continuous --broker full_auto

  # Run single test cycle
  python3 hightrade_orchestrator.py test

  # Check system health
  python3 hightrade_orchestrator.py health

  # Show status
  python3 hightrade_orchestrator.py status
        """
    )

    parser.add_argument(
        'command',
        nargs='?',
        default='continuous',
        choices=['continuous', 'test', 'health', 'setup-email', 'setup-slack', 'status'],
        help='Command to execute'
    )

    parser.add_argument(
        'interval',
        nargs='?',
        type=int,
        default=15,
        help='Interval in minutes for continuous mode (default: 15)'
    )

    parser.add_argument(
        '--broker',
        type=str,
        choices=['disabled', 'semi_auto', 'full_auto'],
        default='semi_auto',
        help='Broker mode: disabled (manual), semi_auto (autonomous with alerts), full_auto (fully autonomous)'
    )

    args = parser.parse_args()

    if args.command == 'continuous':
        ensure_single_orchestrator_instance()

    broker_mode_explicit = '--broker' in sys.argv
    orchestrator = HighTradeOrchestrator(
        broker_mode=args.broker,
        broker_mode_explicit=broker_mode_explicit,
    )

    if args.command == 'health':
        success = orchestrator.check_system_health()
        sys.exit(0 if success else 1)

    elif args.command == 'setup-email':
        orchestrator.setup_email_alerts()

    elif args.command == 'setup-slack':
        orchestrator.setup_slack_alerts()

    elif args.command == 'status':
        orchestrator.print_status_summary()

    elif args.command == 'test':
        orchestrator.run_test()

    elif args.command == 'continuous':
        interval = args.interval if args.interval else 15
        orchestrator.run_continuous(interval_minutes=interval)


if __name__ == '__main__':
    main()
