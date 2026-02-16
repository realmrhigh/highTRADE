#!/usr/bin/env python3
"""
Setup #logs-silent Slack Webhook
Interactive script to configure the silent logging channel
"""

import json
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / 'trading_data' / 'alert_config.json'

def setup_logs_silent():
    print("\n" + "=" * 60)
    print("  HighTrade #logs-silent Slack Webhook Setup")
    print("=" * 60)

    print("""
To get your #logs-silent webhook URL:

  1. Go to: https://api.slack.com/apps
  2. Select your 'HighTrade Broker' app
  3. Click 'Incoming Webhooks' in the left sidebar
  4. Click 'Add New Webhook to Workspace'
  5. Select '#logs-silent' channel
  6. Click 'Allow'
  7. Copy the Webhook URL (starts with https://hooks.slack.com/)
    """)

    webhook_url = input("Paste your #logs-silent Webhook URL: ").strip()

    if not webhook_url:
        print("❌ Setup cancelled")
        return False

    if not webhook_url.startswith('https://hooks.slack.com/'):
        print("❌ Invalid webhook URL. Must start with https://hooks.slack.com/")
        return False

    # Load existing config
    with open(CONFIG_PATH, 'r') as f:
        config = json.load(f)

    # Update webhook URL
    if 'slack_logging' not in config['channels']:
        config['channels']['slack_logging'] = {
            'enabled': True,
            'webhook_url': webhook_url,
            'log_interval_minutes': 15,
            'log_events': [
                'status',
                'defcon_change',
                'trade_entry',
                'trade_exit',
                'monitoring_cycle'
            ]
        }
    else:
        config['channels']['slack_logging']['webhook_url'] = webhook_url
        config['channels']['slack_logging']['enabled'] = True

    # Save config
    with open(CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2)

    print("\n✅ Webhook URL saved to alert_config.json")

    # Test the webhook
    print("\n🧪 Testing #logs-silent webhook...")

    from alerts import AlertSystem
    alerts = AlertSystem()

    result = alerts.send_silent_log('monitoring_cycle', {
        'cycle': 999,
        'defcon_level': 5,
        'signal_score': 2.0,
        'vix': 20.6,
        'bond_yield': 4.09,
        'holdings': 'TEST MESSAGE'
    })

    if result:
        print("✅ Test message sent to #logs-silent successfully!")
        print("\nYou should see a message in the #logs-silent Slack channel.")
        return True
    else:
        print("❌ Test message failed. Check webhook URL and try again.")
        return False

if __name__ == '__main__':
    setup_logs_silent()
