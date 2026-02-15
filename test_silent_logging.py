#!/usr/bin/env python3
"""
Test script for silent logging channel
Run this after setting up the webhook URL in alert_config.json
"""

import sys
from pathlib import Path

# Add current directory to path
sys.path.insert(0, str(Path(__file__).parent.resolve()))

from alerts import AlertSystem

def test_silent_logging():
    """Test the silent logging channel"""
    print("\n" + "="*70)
    print("TESTING SILENT LOGGING CHANNEL")
    print("="*70)

    alerts = AlertSystem()

    # Check if slack_logging is configured
    if 'slack_logging' not in alerts.config['channels']:
        print("\n❌ slack_logging not found in config")
        print("   Run setup first - see setup_silent_logging.md")
        return False

    logging_config = alerts.config['channels']['slack_logging']

    if not logging_config.get('enabled', False):
        print("\n❌ slack_logging is disabled")
        print("   Set 'enabled': true in alert_config.json")
        return False

    webhook_url = logging_config.get('webhook_url', '')
    if 'PLACEHOLDER' in webhook_url or not webhook_url:
        print("\n❌ Webhook URL not configured")
        print("   Replace PLACEHOLDER with actual webhook URL")
        print("   See setup_silent_logging.md for instructions")
        return False

    print("\n✅ Configuration looks good!")
    print(f"   Webhook: {webhook_url[:50]}...")
    print(f"   Enabled: {logging_config['enabled']}")
    print(f"   Log Events: {', '.join(logging_config['log_events'])}")

    # Test different event types
    print("\n" + "-"*70)
    print("SENDING TEST MESSAGES")
    print("-"*70)

    test_events = [
        ('monitoring_cycle', {
            'cycle': 999,
            'defcon_level': 5,
            'signal_score': 42.5,
            'vix': 18.2,
            'bond_yield': 4.15,
            'holdings': 'GOOGL, NVDA, MSFT (TEST)'
        }),
        ('defcon_change', {
            'old_defcon': 5,
            'new_defcon': 2,
            'signal_score': 85.3
        }),
        ('trade_entry', {
            'assets': 'GOOGL, NVDA, MSFT',
            'position_size': 10000,
            'defcon': 2
        }),
        ('trade_exit', {
            'asset': 'GOOGL',
            'reason': 'profit_target',
            'pnl_pct': 5.2
        })
    ]

    success_count = 0
    for event_type, data in test_events:
        print(f"\n📤 Sending {event_type}...")
        result = alerts.send_silent_log(event_type, data)
        if result:
            print(f"   ✅ Success")
            success_count += 1
        else:
            print(f"   ❌ Failed")

    print("\n" + "="*70)
    print(f"RESULTS: {success_count}/{len(test_events)} messages sent successfully")
    print("="*70)

    if success_count == len(test_events):
        print("\n✅ All tests passed!")
        print("   Check your #logs-silent channel in Slack")
        return True
    else:
        print("\n⚠️  Some messages failed")
        print("   Check webhook URL and Slack permissions")
        return False

if __name__ == '__main__':
    success = test_silent_logging()
    sys.exit(0 if success else 1)
