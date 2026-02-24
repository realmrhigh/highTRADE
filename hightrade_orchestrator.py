#!/usr/bin/env python3
"""
HighTrade Orchestrator - Main System Controller
Coordinates database builder, news watcher, and notification system
Runs autonomously to gather data, analyze it, and send alerts
"""

import sys
import logging
import json
from pathlib import Path
from datetime import datetime
import time
from monitoring import SignalMonitor
from alerts import AlertSystem
from dashboard import generate_dashboard_html
from crisis_db_utils import CrisisDatabase
from paper_trading import PaperTradingEngine
from broker_agent import AutonomousBroker
from hightrade_cmd import CommandProcessor
from news_aggregator import NewsAggregator
from news_sentiment import NewsSentimentAnalyzer
from news_signals import NewsSignalGenerator
from config_validator import ConfigValidator

# Configuration
SCRIPT_DIR = Path(__file__).parent.resolve()
DB_PATH = SCRIPT_DIR / 'trading_data' / 'trading_history.db'
LOGS_PATH = SCRIPT_DIR / 'trading_data' / 'logs'
CONFIG_PATH = SCRIPT_DIR / 'trading_data' / 'orchestrator_config.json'

# Create logs directory
LOGS_PATH.mkdir(parents=True, exist_ok=True)

# Set up logging
LOG_FILE = LOGS_PATH / f"hightrade_{datetime.now().strftime('%Y%m%d')}.log"
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class HighTradeOrchestrator:
    """Main orchestrator for HighTrade system"""

    def __init__(self, broker_mode='semi_auto'):
        """Initialize orchestrator components

        broker_mode options:
          - 'disabled': Paper trading only, user approval required
          - 'semi_auto': Alerts sent, trades executed with tips
          - 'full_auto': Complete autonomous trading
        """
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
        self.paper_trading = PaperTradingEngine(DB_PATH, total_capital=100000)

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
                logger.info("📊 FRED Macro Tracker initialized (no API key — add fred_api_key to config)")
        except Exception as e:
            logger.warning(f"⚠️  FRED macro tracker init failed: {e}")
            self.fred = None
            self.fred_enabled = False

        self.previous_defcon = 5
        self.monitoring_cycles = 0
        self.alerts_sent = 0
        self.pending_trade_alerts = []
        self.pending_trade_exits = []
        self._new_interval = None  # Set by /interval command
        self._daily_briefing_date = None  # Track last briefing date
        self._acquisition_pipeline_date = None  # Track last research+analyst run
        self._morning_flash_date = None   # Track morning Flash briefing (9:30 AM)
        self._midday_flash_date  = None   # Track midday Flash briefing (12:00 PM)
        self._health_check_date  = None   # Track twice-weekly health check (Mon + Thu)

        # Slash command processor
        self.cmd_processor = CommandProcessor(self)

        logger.info("✅ Orchestrator initialized successfully")
        logger.info(f"🤖 Broker Mode: {broker_mode.upper()}")
        logger.info(f"📰 News Monitoring: {'ENABLED' if self.news_enabled else 'DISABLED'}")
        logger.info(f"📡 Slash commands: python3 hightrade_cmd.py /help")

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
        Uses the same yfinance/market data source the rest of the system uses.
        Falls back gracefully — a price fetch failure never blocks the cycle.
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
                import yfinance as yf
                ticker = yf.Ticker(sym)
                hist = ticker.history(period='1d', interval='1m')
                if not hist.empty:
                    current_price = float(hist['Close'].iloc[-1])
                else:
                    # Market closed — use last close
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

                    # Persist to DB
                    try:
                        conn = _sqlite3.connect(str(self.paper_trading.db_path))
                        conn.execute("""
                            UPDATE trade_records
                            SET current_price = ?, unrealized_pnl_dollars = ?,
                                unrealized_pnl_percent = ?, last_price_updated = ?,
                                position_size_dollars = entry_price * shares
                            WHERE trade_id = ? AND status = 'open'
                        """, (current_price, upnl_dollars, upnl_pct,
                              _dt.now().isoformat(), p.get('trade_id')))
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
        sector_result = None
        if self.sector_analyzer:
            sector_result = self.sector_analyzer.get_rotation_data()
        
        vix_result = None
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

                        # If fresh news has breaking override, use it instead
                        if fresh_news_signal['breaking_news_override']:
                            logger.warning(f"  🚨 BREAKING NEWS DETECTED: {fresh_news_signal['crisis_description']}")
                            news_signal = fresh_news_signal

                        # Detect new articles BEFORE Gemini — skip LLM when 0 new articles
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

                            # --- LAYER 2: Pro deep analysis + Grok second opinion on elevated signals ---
                            if self.gemini.should_run_pro(score, fresh_news_signal['breaking_count'], defcon_changed):
                                logger.info(f"  🧠 Elevated signal ({score:.1f}) — triggering deep AI analysis (Gemini Pro + Grok)...")
                                open_positions = self.paper_trading.get_open_positions()
                                
                                # 1. Gemini Pro (Standard Tier)
                                gemini_pro_result = self.gemini.run_pro_analysis(
                                    articles_for_gemini,
                                    score_components=components,
                                    sentiment_summary=fresh_news_signal['sentiment_summary'],
                                    crisis_type=fresh_news_signal['dominant_crisis_type'],
                                    news_score=score,
                                    flash_analysis=gemini_flash_result,
                                    current_defcon=self.previous_defcon,
                                    positions=open_positions,
                                    sector_rotation=sector_result,
                                    vix_term_structure=vix_result
                                )
                                
                                # 2. Grok Second Opinion (X.com Analysis)
                                if self.grok_enabled:
                                    grok_result = self.grok.run_analysis(
                                        articles_for_gemini,
                                        crisis_type=fresh_news_signal['dominant_crisis_type'],
                                        news_score=score,
                                        sector_rotation=sector_result,
                                        vix_term_structure=vix_result,
                                        positions=open_positions,
                                        current_defcon=self.previous_defcon
                                    )
                                    
                                    # --- VETO / CONTRARIAN FLAG LOGIC ---
                                    if grok_result and gemini_pro_result:
                                        gemini_action = gemini_pro_result.get('recommended_action', 'WAIT').upper()
                                        grok_action = grok_result.get('second_opinion_action', 'WAIT').upper()
                                        
                                        if (gemini_action in ('BUY', 'SELL')) and (grok_action != gemini_action):
                                            logger.warning(f"  🚨 AI DISAGREEMENT: Gemini={gemini_action}, Grok={grok_action}. Flagging for review.")
                                            grok_result['disagreement_flag'] = True
                                else:
                                    grok_result = None
                        elif self.gemini_enabled:
                            logger.info(f"  ⏭️  Skipping Gemini — 0 new articles (reusing previous analysis)")

                        # Store full signal with Gemini Flash embedded
                        signal_id = self._record_news_signal(
                            fresh_news_signal,
                            articles_full=articles,
                            gemini_flash=gemini_flash_result
                        )

                        # Save Pro analysis to gemini_analysis table
                        if gemini_pro_result and signal_id and self.gemini_enabled:
                            self.gemini.save_analysis_to_db(
                                str(DB_PATH), signal_id, gemini_pro_result,
                                trigger_type='elevated' if score >= 40 else 'breaking'
                            )
                            
                        # Save Grok analysis to grok_analysis table
                        if grok_result and signal_id and self.grok_enabled:
                            self.grok.save_analysis_to_db(
                                str(DB_PATH), signal_id, grok_result
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
                                
                            grok_summary = None
                            if 'grok_result' in locals() and grok_result:
                                grok_summary = {
                                    'action': grok_result.get('second_opinion_action', 'WAIT'),
                                    'x_sentiment': grok_result.get('x_sentiment_score', 0),
                                    'reasoning': grok_result.get('reasoning', '')[:200],
                                    'disagreement_flag': grok_result.get('disagreement_flag', False)
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
                                'grok': grok_summary,
                                'timestamp': datetime.now().isoformat()
                            })
                            
                            # --- 🐕 GROK HOUND (Every cycle alpha scanner) ---
                            if self.hound_enabled:
                                try:
                                    hound_state = {
                                        "defcon_level": self.monitor.defcon_level,
                                        "macro_score": macro_result.get('macro_score', 50) if macro_result else 50,
                                        "watchlist": [p.get('symbol') for p in self.paper_trading.get_open_positions()],
                                        "latest_gemini_briefing_summary": gemini_summary.get('reasoning', '') if gemini_summary else ""
                                    }
                                    hound_results = self.hound.hunt(hound_state)
                                    self.hound.save_candidates(hound_results)
                                    
                                    # Alert Slack if any high-potential memes found (score > 75)
                                    for candidate in hound_results.get('candidates', []):
                                        if candidate.get('meme_score', 0) >= 75:
                                            self.alerts.send_notify('hound_alert', {
                                                'ticker': candidate['ticker'],
                                                'score': candidate['meme_score'],
                                                'thesis': candidate['why_next_gme'],
                                                'risks': candidate['risks'],
                                                'action': candidate['action_suggestion']
                                            })
                                except Exception as e:
                                    logger.warning(f"  🐕 Grok Hound failed: {e}")
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
                            f"  🎯 TOP CLUSTER: {top['ticker']} — "
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

            # Calculate and record
            logger.info("📈 Calculating signal scores...")
            signal_scores = self.monitor.calculate_signal_scores(yield_data, vix_data, market_data)
            current_defcon, signal_score = self.monitor.calculate_defcon_level(signal_scores, market_data, news_signal)

            logger.info(f"  📊 Bond Yield Spike Score: {signal_scores.get('bond_yield_spike', 0):.1f}")
            logger.info(f"  📊 VIX Spike Score: {signal_scores.get('vix_spike', 0):.1f}")
            logger.info(f"  📊 Market Drawdown Score: {signal_scores.get('market_drawdown', 0):.1f}")
            logger.info(f"  📊 Composite Score: {signal_score:.1f}/100")

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

            # Send alerts if DEFCON changed or escalated
            if current_defcon != self.previous_defcon:
                old_defcon = self.previous_defcon
                self.previous_defcon = current_defcon  # Always update — fixes de-escalation blindness

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

                # NEW: Broker agent decides on trades (DEFCON 1-2 only)
                if self.cmd_processor.should_skip_trades:
                    logger.warning("⏸️  Trading on HOLD — skipping trade execution")
                elif current_defcon <= 2:
                    crisis_desc = f"DEFCON {current_defcon} escalation - Signal Score: {signal_score:.1f}"
                    market_conditions = {'vix': vix} if vix else {}

                    if self.broker_mode == 'disabled':
                        # Manual mode: generate alert for user approval
                        trade_alert = self.paper_trading.generate_trade_alert(
                            defcon_level=current_defcon,
                            signal_score=signal_score,
                            crisis_description=crisis_desc,
                            market_data=market_conditions
                        )

                        logger.info("\n" + "="*60)
                        logger.info("🎯 TRADE ALERT (Multi-Asset Package)")
                        logger.info("="*60)
                        logger.info(f"Crisis Type: {trade_alert['crisis_type']}")
                        logger.info(f"Primary Asset: {trade_alert['assets']['primary_asset']} (50% - ${trade_alert['assets']['primary_size']:,.0f})")
                        logger.info(f"Secondary Asset: {trade_alert['assets']['secondary_asset']} (30% - ${trade_alert['assets']['secondary_size']:,.0f})")
                        logger.info(f"Tertiary Asset: {trade_alert['assets']['tertiary_asset']} (20% - ${trade_alert['assets']['tertiary_size']:,.0f})")
                        logger.info(f"Total Position Size: ${trade_alert['total_position_size']:,.0f}")
                        logger.info(f"Confidence Score: {trade_alert['confidence_score']}/100")
                        logger.info(f"VIX: {trade_alert['vix']:.1f}")
                        logger.info(f"Rationale: {trade_alert['rationale']}")
                        logger.info(f"Risk/Reward: {trade_alert['risk_reward_analysis']}")
                        logger.info(f"Approval Window: {trade_alert['time_window_minutes']} minutes")
                        logger.info("="*60 + "\n")

                        self.pending_trade_alerts.append(trade_alert)

                    else:
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
                            logger.info("ℹ️  BROKER: Trade criteria not met or daily limit reached")

                # Monitor and process exits (respects hold)
                if not self.cmd_processor.should_skip_trades:
                    if self.broker_mode == 'disabled':
                        self.monitor_and_exit_positions()
                    else:
                        exits = self.broker.process_exits()
                        if exits > 0:
                            logger.info(f"✅ BROKER: {exits} position(s) exited autonomously")

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

            # ── Acquisition conditionals (every cycle, any broker mode except disabled) ──
            if self.broker_mode != 'disabled':
                try:
                    live_state = {
                        'defcon': current_defcon,
                        'news_score': locals().get('score') or 0,
                        'macro_score': self._get_latest_macro_score(),
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

                # Calculate live portfolio value: cash + current market value of open positions
                total_capital = self.paper_trading.total_capital
                realized_pnl  = perf.get('total_profit_loss_dollars', 0)
                unrealized_pnl = sum(p.get('unrealized_pnl_dollars') or 0 for p in open_positions)
                deployed = sum(
                    (p.get('current_price') or p.get('entry_price', 0)) * p.get('shares', 0)
                    for p in open_positions
                )
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

        # ── Intraday Flash Briefings (morning 9:30 AM, midday 12:00 PM) ───
        self._check_flash_briefings()

        # ── Daily Briefing (fires once per day at/after 4:30 PM) ──────────
        self._check_daily_briefing()

        # ── Bi-weekly Health Check (Thursdays only, once per week pair) ───
        self._check_health_agent()

    def _check_health_agent(self):
        """
        Twice-weekly system health check — fires on Mondays and Thursdays,
        throttled to at most once per 3 days by health_agent's internal state.
        Checks APIs, monitoring recency, recurring data gaps, and new Gemini models.
        Results sent to #all-hightrade via send_notify().
        """
        now = datetime.now()
        # Only run on Monday (0) or Thursday (3)
        if now.weekday() not in (0, 3):
            return
        today = now.strftime('%Y-%m-%d')
        if self._health_check_date == today:
            return  # already ran today
        self._health_check_date = today

        day_name = 'Monday' if now.weekday() == 0 else 'Thursday'
        logger.info(f"🏥 {day_name} health check — running twice-weekly system audit...")
        try:
            from health_agent import run_and_notify
            result = run_and_notify(self.alerts, force=False)
            if result:
                logger.info(f"  ✅ Health check complete: {result.get('summary', '')}")
            # 'skipped' means <3 days since last run — silently move on
        except Exception as e:
            logger.warning(f"  ⚠️  Health agent failed: {e}")

    def _check_flash_briefings(self):
        """
        Fire lightweight Gemini Flash briefings at two intraday checkpoints:
          • Morning  — 9:30 AM ET  (market open snapshot)
          • Midday   — 12:00 PM ET (lunch check-in)

        Each fires once per calendar day. Results saved to daily_briefings table
        (model_key = 'morning_flash' / 'midday_flash') so the 4:30 PM Pro synthesis
        has structured intraday context. Summary also sent to #logs-silent.
        """
        now   = datetime.now()
        today = now.strftime('%Y-%m-%d')
        hour  = now.hour
        minute = now.minute

        windows = [
            ('morning', 9,  30,  '_morning_flash_date', '🌅 Morning'),
            ('midday',  12,  0,  '_midday_flash_date',  '☀️  Midday'),
        ]

        for label, tgt_hour, tgt_min, attr, emoji in windows:
            past_window = (hour > tgt_hour or (hour == tgt_hour and minute >= tgt_min))
            already_ran = getattr(self, attr) == today
            if not past_window or already_ran:
                continue

            setattr(self, attr, today)
            logger.info(f"📊 {emoji} Flash briefing firing ({tgt_hour:02d}:{tgt_min:02d})...")
            try:
                self._run_flash_briefing(label, emoji)
            except Exception as e:
                logger.warning(f"{emoji} Flash briefing failed: {e}")

    def _run_flash_briefing(self, label: str, emoji: str):
        """Build a concise Flash prompt from live state and send summary to #logs-silent."""
        import sqlite3 as _sq

        # ── Gather context ────────────────────────────────────────────────
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M')

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

        # Open positions — enriched with live prices and unrealized P&L
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

        # Active conditionals — with live price and distance to trigger
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

            # Batch-fetch all unique tickers in one yfinance call
            cond_tickers = list({r[0] for r in cond_rows})
            if cond_tickers:
                raw = _yf.download(
                    cond_tickers, period='1d', interval='1m',
                    group_by='ticker', progress=False, auto_adjust=True
                )

            def _live_price(sym):
                try:
                    if len(cond_tickers) == 1:
                        return float(raw['Close'].iloc[-1])
                    return float(raw[sym]['Close'].iloc[-1])
                except Exception:
                    return None

            cond_lines = []
            for r in cond_rows:
                sym, target, tag, stop, tp1, thesis = r
                live = _live_price(sym)
                if live is not None:
                    dist_pct = (live - target) / target * 100
                    arrow    = "🔴 ABOVE target" if live > target else f"📍 {abs(dist_pct):.1f}% away"
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

        # ── Build prompt ─────────────────────────────────────────────────
        prompt = f"""You are HighTrade's intraday market analyst. Today is {now_str}.
Provide a BRIEF {label} market check-in — 4 to 6 sentences max. Be direct and actionable.

CURRENT STATE:
  DEFCON: {defcon}/5
  Macro Score: {macro_score:.0f}/100
  News: {news_ctx}

OPEN POSITIONS (live prices + unrealized P&L):
{pos_ctx}

ACTIVE CONDITIONALS (live price vs entry target):
{cond_ctx}

OUTPUT FORMAT (plain text, no JSON needed):
1. One sentence on overall market tone right now.
2. Any immediate risks or catalysts to watch.
3. Open positions — are they behaving as expected given entry vs current price and P&L?
4. Which conditionals are closest to triggering (within ~1-2% of target)?
5. One actionable takeaway for the next few hours.
DATA GAPS: On a final line starting with "GAPS:" list any specific data that was missing or would have sharpened this analysis — e.g. "GAPS: real-time sector rotation, VIX term structure, MSFT options flow". If nothing is missing write "GAPS: none"."""

        from gemini_client import call as gemini_call
        text, in_tok, out_tok = gemini_call(prompt, model_key='fast')

        if not text:
            logger.warning(f"{emoji} Flash briefing: no response from Gemini")
            return

        # ── Parse GAPS: line from end of response ────────────────────────
        gaps_list = []
        summary_text = text
        lines = text.strip().splitlines()
        for i, line in enumerate(lines):
            if line.strip().upper().startswith('GAPS:'):
                gaps_raw = line.split(':', 1)[1].strip()
                if gaps_raw.lower() != 'none':
                    gaps_list = [g.strip() for g in gaps_raw.split(',') if g.strip()]
                # Remove GAPS line from display summary
                summary_text = '\n'.join(lines[:i] + lines[i+1:]).strip()
                break

        logger.info(f"  {emoji} Flash ({in_tok}→{out_tok} tok): {summary_text[:120]}...")
        if gaps_list:
            logger.info(f"  🔍 Flash data gaps: {' | '.join(gaps_list)}")

        # ── Write to daily_briefings DB so Pro end-of-day has structured intraday context ──
        date_str = datetime.now().strftime('%Y-%m-%d')
        model_key_db = f"{label.lower().replace(' ', '_')}_flash"  # e.g. 'morning_flash', 'midday_flash'
        try:
            import sqlite3 as _sq
            conn = _sq.connect(str(DB_PATH))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_briefings (
                    briefing_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    date             TEXT NOT NULL,
                    model_key        TEXT NOT NULL,
                    model_id         TEXT,
                    market_regime    TEXT,
                    regime_confidence REAL,
                    headline_summary TEXT,
                    key_themes_json  TEXT,
                    biggest_risk     TEXT,
                    biggest_opportunity TEXT,
                    signal_quality   TEXT,
                    macro_alignment  TEXT,
                    congressional_alpha TEXT,
                    portfolio_assessment TEXT,
                    watchlist_json   TEXT,
                    entry_conditions TEXT,
                    defcon_forecast  TEXT,
                    reasoning_chain  TEXT,
                    model_confidence REAL,
                    input_tokens     INTEGER,
                    output_tokens    INTEGER,
                    full_response_json TEXT,
                    data_gaps_json   TEXT,
                    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(date, model_key)
                )
            """)
            conn.execute("""
                INSERT OR REPLACE INTO daily_briefings
                (date, model_key, model_id, headline_summary,
                 macro_alignment, input_tokens, output_tokens,
                 full_response_json, data_gaps_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                date_str,
                model_key_db,
                'gemini-2.5-flash',
                summary_text,
                f"DEFCON {defcon}/5 | Macro {macro_score:.0f}/100",
                in_tok,
                out_tok,
                json.dumps({'label': label, 'defcon': defcon,
                            'macro_score': macro_score, 'summary': summary_text,
                            'gaps': gaps_list}),
                json.dumps(gaps_list) if gaps_list else None,
            ))
            conn.commit()
            conn.close()
            logger.info(f"  💾 {emoji} Flash briefing saved to daily_briefings ({model_key_db})")
        except Exception as db_err:
            logger.warning(f"  ⚠️  Flash briefing DB write failed: {db_err}")

        # Send to #all-hightrade (push notification) and mirror to #logs-silent
        payload = {
            'label':      label,
            'emoji':      emoji,
            'summary':    summary_text,
            'gaps':       gaps_list,
            'defcon':     defcon,
            'macro_score': macro_score,
            'in_tokens':  in_tok,
            'out_tokens': out_tok,
        }
        self.alerts.send_notify('flash_briefing', payload)
        self.alerts.send_silent_log('flash_briefing', payload)

    def _check_daily_briefing(self, force: bool = False):
        """Fire daily briefing once per day after market close (4:30 PM ET)."""
        try:
            now = datetime.now()
            today = now.strftime('%Y-%m-%d')
            market_close_hour = 16  # 4 PM — briefing triggers at 4:30
            market_close_minute = 30

            # Only fire after 4:30 PM and only once per date
            after_close = (now.hour > market_close_hour or
                           (now.hour == market_close_hour and now.minute >= market_close_minute))

            if not force and (not after_close or self._daily_briefing_date == today):
                return

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

            # Trigger acquisition pipeline after briefing (researcher then analyst)
            # The briefing has already queued new tickers into acquisition_watchlist.
            # Run these with a 60-second delay to not overlap Gemini calls.
            self._run_acquisition_pipeline(today)

        except Exception as e:
            logger.warning(f"Daily briefing failed: {e}")

    def _run_acquisition_pipeline(self, date_str: str):
        """
        Run the acquisition research → analyst pipeline once per day.

        Called automatically after the daily briefing fires.
        Researcher collects yfinance + SEC + internal data on pending tickers,
        then Analyst runs Gemini 3 Pro to set conditionals above confidence threshold.
        The Flash verifier already ran as part of daily_briefing._save_to_db.
        """
        if self._acquisition_pipeline_date == date_str:
            return  # Already ran today

        logger.info("🔬 Starting acquisition pipeline: researcher → analyst...")
        self._acquisition_pipeline_date = date_str

        import time as _time

        # Step 1: Researcher — gather all data
        try:
            from acquisition_researcher import run_research_cycle
            researched = run_research_cycle()
            logger.info(f"  📚 Researcher: {len(researched)} tickers ready → {researched}")
        except Exception as e:
            logger.error(f"  ❌ Acquisition researcher failed: {e}")
            return

        if not researched:
            logger.info("  📭 No tickers to analyze")
            return

        # Brief pause so we don't slam Gemini back-to-back with Pro calls
        _time.sleep(10)

        # Step 2: Analyst — set conditionals via Gemini 3 Pro
        try:
            from acquisition_analyst import run_analyst_cycle
            results = run_analyst_cycle()
            promoted = [
                r.get('_ticker') for r in results
                if r.get('should_enter') and r.get('research_confidence', 0) >= 0.7
            ]
            logger.info(
                f"  🧠 Analyst: {len(results)} analyzed, "
                f"{len(promoted)} promoted to broker → {promoted}"
            )
        except Exception as e:
            logger.error(f"  ❌ Acquisition analyst failed: {e}")

        logger.info("✅ Acquisition pipeline complete")

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

    def run_continuous(self, interval_minutes=15):
        """Run system continuously with slash command support"""
        logger.info(f"\n🚀 Starting HighTrade in continuous mode")
        logger.info(f"   Interval: {interval_minutes} minutes")
        logger.info(f"   Log: {LOG_FILE}")
        logger.info(f"   Commands: python3 hightrade_cmd.py /help")

        cycle = 0
        try:
            while True:
                # Check for commands before each cycle
                self.cmd_processor.check_for_commands()

                # Respect stop commands
                if self.cmd_processor.should_stop:
                    logger.info("🛑 Stop command received — shutting down")
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
        try:
            import sqlite3
            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.cursor()
            cursor.execute("""
                SELECT macro_score FROM macro_indicators
                ORDER BY timestamp DESC LIMIT 1
            """)
            row = cursor.fetchone()
            conn.close()
            return float(row[0]) if row else 50.0
        except Exception:
            return 50.0  # neutral fallback — don't block gate on DB error

    def _check_breaking_news_in_db(self):
        """Check database for recent breaking news signals (within last 4 hours)"""
        try:
            import sqlite3
            from datetime import datetime, timedelta
            
            logger.info("  🔍 Checking database for breaking news...")
            conn = sqlite3.connect(str(DB_PATH))
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
            conn.close()
            
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

    def _record_news_signal(self, news_signal, articles_full=None, gemini_flash=None):
        """Store news signal in database with full rich data for LLM access"""
        try:
            import sqlite3
            conn = sqlite3.connect(str(DB_PATH))
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
            conn.close()
            logger.debug(f"News signal recorded to database (ID={signal_id})")
            return signal_id

        except Exception as e:
            logger.error(f"Failed to record news signal: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None

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
        try:
            import sqlite3
            from datetime import datetime, timedelta

            conn = sqlite3.connect(str(DB_PATH))
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
            conn.close()

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

    def monitor_and_exit_positions(self):
        """Monitor all open positions and detect exit conditions"""
        exit_recommendations = self.paper_trading.monitor_all_positions()

        if exit_recommendations:
            logger.info("\n" + "="*60)
            logger.info("⚠️  EXIT SIGNALS DETECTED")
            logger.info("="*60)

            for exit_rec in exit_recommendations:
                logger.info(f"{exit_rec['message']}")
                self.pending_trade_exits.append(exit_rec)

            logger.info("="*60 + "\n")

    def execute_pending_trades(self, auto_approve=False):
        """
        Execute pending trade alerts

        auto_approve: If True, automatically approve all pending trades
                      If False, require manual approval for each
        """
        if not self.pending_trade_alerts:
            logger.info("No pending trade alerts")
            return []

        executed_trades = []

        for alert in self.pending_trade_alerts:
            logger.info(f"\n📋 Approving trade package:")
            logger.info(f"   Primary: {alert['assets']['primary_asset']}")
            logger.info(f"   Secondary: {alert['assets']['secondary_asset']}")
            logger.info(f"   Tertiary: {alert['assets']['tertiary_asset']}")
            logger.info(f"   Size: ${alert['total_position_size']:,.0f}")

            if auto_approve:
                logger.info("   ✅ Auto-approved")
                approval = True
            else:
                response = input("   Execute trade? (y/n): ").strip().lower()
                approval = response == 'y'

            if approval:
                trade_ids = self.paper_trading.execute_trade_package(alert, user_approval=True)
                executed_trades.extend(trade_ids)
                logger.info(f"   ✅ EXECUTED - Trade IDs: {trade_ids}")
            else:
                logger.info("   ❌ Skipped by user")

        self.pending_trade_alerts = []
        return executed_trades

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
        default='disabled',
        help='Broker mode: disabled (manual), semi_auto (autonomous with alerts), full_auto (fully autonomous)'
    )

    args = parser.parse_args()

    orchestrator = HighTradeOrchestrator(broker_mode=args.broker)

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
