#!/usr/bin/env python3
"""
Quick Slack Setup for HighTrade Broker
Interactive script to configure Slack notifications in under 1 minute
"""

import json
from pathlib import Path

CONFIG_PATH = Path.home() / 'trading_data' / 'alert_config.json'

def setup_slack():
    """Interactive Slack setup"""
    print("\n" + "="*60)
    print("🤖 HighTrade Broker - Slack Setup (1 minute)")
    print("="*60)

    print("\n📖 Getting your Slack Webhook URL:")
    print("   1. Go to: https://api.slack.com/apps")
    print("   2. Click 'Create New App' → 'From scratch'")
    print("   3. Name: 'HighTrade Broker'")
    print("   4. Select your workspace")
    print("   5. Click 'Incoming Webhooks' (left sidebar)")
    print("   6. Toggle 'Activate Incoming Webhooks' ON")
    print("   7. Click 'Add New Webhook to Workspace'")
    print("   8. Select channel (e.g., #trading)")
    print("   9. Click 'Allow'")
    print("   10. Copy the Webhook URL\n")

    webhook_url = input("🔗 Paste your Slack Webhook URL: ").strip()

    if not webhook_url:
        print("❌ Setup cancelled - no webhook URL provided")
        return False

    if not webhook_url.startswith('https://hooks.slack.com/'):
        print("❌ Invalid webhook URL. Must start with: https://hooks.slack.com/")
        return False

    # Load current config
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, 'r') as f:
            config = json.load(f)
    else:
        print(f"⚠️  Config file not found at {CONFIG_PATH}")
        return False

    # Update Slack settings
    config['channels']['slack']['enabled'] = True
    config['channels']['slack']['webhook_url'] = webhook_url

    # Ensure alert thresholds are set
    config['alert_thresholds']['defcon_2'] = True
    config['alert_thresholds']['defcon_1'] = True

    # Save config
    try:
        with open(CONFIG_PATH, 'w') as f:
            json.dump(config, f, indent=2)
        print("\n✅ Slack webhook configured successfully!")
    except Exception as e:
        print(f"\n❌ Failed to save config: {e}")
        return False

    # Test the webhook
    print("\n🧪 Testing Slack webhook...")
    test_slack_webhook(webhook_url)

    print("\n" + "="*60)
    print("🎉 Setup Complete!")
    print("="*60)
    print("\n💡 Next steps:")
    print("   1. Start your broker:")
    print("      python3 hightrade_orchestrator.py continuous --broker semi_auto")
    print("\n   2. Monitor in Slack:")
    print("      • Each trade decision sends a notification")
    print("      • Buy signals show assets and sizing")
    print("      • Sell signals show profit/loss")
    print("\n   3. Check portfolio anytime:")
    print("      python3 trading_cli.py status")
    print("\n📚 See documentation:")
    print("   • SLACK_SETUP_GUIDE.md")
    print("   • BROKER_GUIDE.md")
    print("\n")

    return True


def test_slack_webhook(webhook_url):
    """Test the Slack webhook"""
    import requests
    from datetime import datetime

    try:
        payload = {
            'attachments': [
                {
                    'color': '#28a745',
                    'title': '🚀 HighTrade Slack Setup Success!',
                    'text': f'Your broker notifications are working!\n\nSetup completed: {datetime.now().isoformat()}',
                    'footer': 'HighTrade Broker',
                    'ts': int(datetime.now().timestamp())
                }
            ]
        }

        response = requests.post(webhook_url, json=payload, timeout=5)

        if response.status_code == 200:
            print("✅ Test message sent to Slack!")
            return True
        else:
            print(f"⚠️  Slack returned status {response.status_code}")
            return False

    except requests.exceptions.Timeout:
        print("⚠️  Connection timeout - check your internet connection")
        return False
    except requests.exceptions.RequestException as e:
        print(f"⚠️  Failed to connect to Slack: {e}")
        return False
    except Exception as e:
        print(f"⚠️  Error testing webhook: {e}")
        return False


if __name__ == '__main__':
    import sys
    success = setup_slack()
    sys.exit(0 if success else 1)
