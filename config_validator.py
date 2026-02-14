#!/usr/bin/env python3
"""
Configuration Validator & Startup Health Check
Validates all API keys, tokens, webhooks and performs connectivity tests
"""

import os
import sys
import json
import sqlite3
import logging
from pathlib import Path
from typing import Dict, List, Tuple
from datetime import datetime

# Optional imports with fallback
try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

try:
    from slack_sdk import WebClient
    SLACK_SDK_AVAILABLE = True
except ImportError:
    SLACK_SDK_AVAILABLE = False

logger = logging.getLogger(__name__)


class ConfigValidator:
    """Validates configuration and performs startup health checks"""

    def __init__(self, data_dir: Path = None):
        self.data_dir = data_dir or Path(__file__).parent / 'trading_data'
        self.alert_config_path = self.data_dir / 'alert_config.json'
        self.db_path = self.data_dir / 'trading_history.db'
        self.errors = []
        self.warnings = []
        self.successes = []

    def validate_all(self) -> bool:
        """Run all validation checks. Returns True if all critical checks pass."""
        print("🔍 Starting HighTrade Configuration Validation...")
        print("=" * 60)

        # Critical checks (must pass)
        self._check_data_directory()
        self._check_database()
        self._check_alert_config()
        
        # API/Service checks (warnings only)
        self._check_slack_config()
        self._check_alpha_vantage()
        self._check_reddit()
        
        # Network checks
        self._check_network_connectivity()
        
        # Display results
        self._display_results()
        
        # Return True only if no critical errors
        return len(self.errors) == 0

    def _check_data_directory(self):
        """Verify data directory exists and is writable"""
        try:
            if not self.data_dir.exists():
                self.errors.append(f"Data directory not found: {self.data_dir}")
                return
            
            # Test write permissions
            test_file = self.data_dir / '.write_test'
            test_file.write_text('test')
            test_file.unlink()
            
            self.successes.append(f"✓ Data directory: {self.data_dir}")
        except Exception as e:
            self.errors.append(f"Data directory not writable: {e}")

    def _check_database(self):
        """Verify database exists and has required tables"""
        try:
            if not self.db_path.exists():
                self.errors.append(f"Database not found: {self.db_path}")
                return
            
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.cursor()
            
            # Check for required tables
            required_tables = [
                'signal_monitoring', 'trade_records', 'defcon_history',
                'crisis_events', 'news_signals', 'claude_analysis'
            ]
            
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            existing_tables = [row[0] for row in cursor.fetchall()]
            
            missing_tables = set(required_tables) - set(existing_tables)
            if missing_tables:
                self.errors.append(f"Missing database tables: {', '.join(missing_tables)}")
            else:
                self.successes.append(f"✓ Database schema valid ({len(existing_tables)} tables)")
            
            # Check database size
            db_size = self.db_path.stat().st_size
            self.successes.append(f"✓ Database size: {db_size / 1024:.1f} KB")
            
            conn.close()
        except Exception as e:
            self.errors.append(f"Database check failed: {e}")

    def _check_alert_config(self):
        """Verify alert configuration file"""
        try:
            if not self.alert_config_path.exists():
                self.warnings.append("⚠ Alert config not found (will use defaults)")
                return
            
            with open(self.alert_config_path) as f:
                config = json.load(f)
            
            # Validate structure
            if 'channels' not in config:
                self.errors.append("Alert config missing 'channels' section")
                return
            
            self.successes.append("✓ Alert configuration loaded")
            
        except json.JSONDecodeError as e:
            self.errors.append(f"Alert config JSON invalid: {e}")
        except Exception as e:
            self.errors.append(f"Alert config check failed: {e}")

    def _check_slack_config(self):
        """Validate Slack configuration and test connectivity"""
        try:
            if not self.alert_config_path.exists():
                return
            
            with open(self.alert_config_path) as f:
                config = json.load(f)
            
            slack_config = config.get('channels', {}).get('slack', {})
            
            if not slack_config.get('enabled'):
                self.warnings.append("⚠ Slack disabled in config")
                return
            
            # Check webhook URL
            webhook_url = slack_config.get('webhook_url', '')
            if not webhook_url or webhook_url == '':
                self.warnings.append("⚠ Slack webhook URL not configured")
            elif webhook_url.startswith('https://hooks.slack.com'):
                self.successes.append("✓ Slack webhook URL configured")
                
                # Test webhook if requests available
                if REQUESTS_AVAILABLE:
                    self._test_slack_webhook(webhook_url)
            
            # Check bot token
            bot_token = slack_config.get('bot_token', '')
            if not bot_token or bot_token == '':
                self.warnings.append("⚠ Slack bot token not configured")
            elif bot_token.startswith('xoxb-'):
                self.successes.append("✓ Slack bot token configured")
                
                # Test bot token if SDK available
                if SLACK_SDK_AVAILABLE:
                    self._test_slack_bot(bot_token)
            
        except Exception as e:
            self.warnings.append(f"⚠ Slack config check failed: {e}")

    def _test_slack_webhook(self, webhook_url: str):
        """Test Slack webhook connectivity"""
        try:
            response = requests.post(
                webhook_url,
                json={"text": "🔧 HighTrade config validator test"},
                timeout=5
            )
            if response.status_code == 200:
                self.successes.append("✓ Slack webhook connectivity OK")
            else:
                self.warnings.append(f"⚠ Slack webhook returned {response.status_code}")
        except requests.exceptions.Timeout:
            self.warnings.append("⚠ Slack webhook timeout (network slow?)")
        except Exception as e:
            self.warnings.append(f"⚠ Slack webhook test failed: {e}")

    def _test_slack_bot(self, bot_token: str):
        """Test Slack bot token"""
        try:
            client = WebClient(token=bot_token)
            response = client.auth_test()
            if response['ok']:
                self.successes.append(f"✓ Slack bot authenticated as {response.get('user', 'unknown')}")
            else:
                self.warnings.append("⚠ Slack bot auth failed")
        except Exception as e:
            self.warnings.append(f"⚠ Slack bot test failed: {e}")

    def _check_alpha_vantage(self):
        """Check Alpha Vantage API key"""
        api_key = os.getenv('ALPHA_VANTAGE_API_KEY', '')
        
        if not api_key:
            self.warnings.append("⚠ ALPHA_VANTAGE_API_KEY not set (news source disabled)")
            return
        
        if api_key == 'demo':
            self.warnings.append("⚠ Using demo Alpha Vantage key (rate limited)")
            return
        
        self.successes.append("✓ Alpha Vantage API key configured")
        
        # Test API if requests available
        if REQUESTS_AVAILABLE:
            self._test_alpha_vantage(api_key)

    def _test_alpha_vantage(self, api_key: str):
        """Test Alpha Vantage API connectivity"""
        try:
            url = f"https://www.alphavantage.co/query?function=NEWS_SENTIMENT&apikey={api_key}&limit=1"
            response = requests.get(url, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                if 'feed' in data:
                    self.successes.append("✓ Alpha Vantage API connectivity OK")
                elif 'Note' in data:
                    self.warnings.append("⚠ Alpha Vantage rate limit reached")
                else:
                    self.warnings.append(f"⚠ Alpha Vantage unexpected response")
            else:
                self.warnings.append(f"⚠ Alpha Vantage returned {response.status_code}")
        except requests.exceptions.Timeout:
            self.warnings.append("⚠ Alpha Vantage timeout (network slow?)")
        except Exception as e:
            self.warnings.append(f"⚠ Alpha Vantage test failed: {e}")

    def _check_reddit(self):
        """Check Reddit credentials"""
        client_id = os.getenv('REDDIT_CLIENT_ID', '')
        client_secret = os.getenv('REDDIT_CLIENT_SECRET', '')
        
        if not client_id or not client_secret:
            self.warnings.append("⚠ Reddit credentials not set (sentiment source limited)")
            return
        
        self.successes.append("✓ Reddit credentials configured")

    def _check_network_connectivity(self):
        """Test basic network connectivity"""
        if not REQUESTS_AVAILABLE:
            self.warnings.append("⚠ requests module not available (skipping network tests)")
            return
        
        try:
            # Test internet connectivity
            response = requests.get('https://www.google.com', timeout=5)
            if response.status_code == 200:
                self.successes.append("✓ Network connectivity OK")
            else:
                self.warnings.append("⚠ Network connectivity issues detected")
        except requests.exceptions.Timeout:
            self.warnings.append("⚠ Network timeout (slow connection?)")
        except Exception as e:
            self.warnings.append(f"⚠ Network check failed: {e}")

    def _display_results(self):
        """Display validation results"""
        print()
        print("=" * 60)
        print("VALIDATION RESULTS")
        print("=" * 60)
        
        if self.successes:
            print(f"\n✅ PASSED ({len(self.successes)}):")
            for success in self.successes:
                print(f"   {success}")
        
        if self.warnings:
            print(f"\n⚠️  WARNINGS ({len(self.warnings)}):")
            for warning in self.warnings:
                print(f"   {warning}")
        
        if self.errors:
            print(f"\n❌ ERRORS ({len(self.errors)}):")
            for error in self.errors:
                print(f"   {error}")
            print()
            print("⛔ CRITICAL ERRORS DETECTED - System may not function correctly")
        else:
            print()
            print("🎉 All critical checks passed! System ready to run.")
        
        print("=" * 60)

    def get_summary(self) -> Dict:
        """Get validation summary as dict"""
        return {
            'timestamp': datetime.now().isoformat(),
            'passed': len(self.errors) == 0,
            'successes': len(self.successes),
            'warnings': len(self.warnings),
            'errors': len(self.errors),
            'details': {
                'successes': self.successes,
                'warnings': self.warnings,
                'errors': self.errors
            }
        }


def main():
    """Run validation from command line"""
    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(levelname)s: %(message)s'
    )
    
    # Run validation
    validator = ConfigValidator()
    passed = validator.validate_all()
    
    # Exit with appropriate code
    sys.exit(0 if passed else 1)


if __name__ == '__main__':
    main()
