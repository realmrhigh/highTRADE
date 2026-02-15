# Setup Silent Logging Channel

## Step 1: Create Incoming Webhook for #logs-silent

1. Go to your Slack workspace settings
2. Navigate to "Apps" → "Manage Apps" → "Custom Integrations"
3. Click "Incoming Webhooks" → "Add Configuration"
4. Select the **#logs-silent** channel
5. Copy the webhook URL (looks like: `https://hooks.slack.com/services/T.../B.../...`)

## Step 2: Update Configuration

Edit `trading_data/alert_config.json`:

```json
"slack_logging": {
  "enabled": true,
  "webhook_url": "YOUR_LOGS_SILENT_WEBHOOK_URL_HERE",
  "log_interval_minutes": 15,
  "log_events": ["monitoring_cycle", "defcon_change", "trade_entry", "trade_exit"]
}
```

Replace `YOUR_LOGS_SILENT_WEBHOOK_URL_HERE` with the webhook URL from Step 1.

## Step 3: Restart Services

```bash
# Restart orchestrator to load new config
sudo launchctl stop com.hightrade.orchestrator

# The LaunchD daemon will auto-restart it within seconds
```

## What Gets Logged

Every 15 minutes (or at your configured interval), the system will post to #logs-silent:

- 🔄 **Monitoring Cycle**: DEFCON, Signal Score, VIX, Bond Yield, Holdings
- 🚨 **DEFCON Changes**: When DEFCON level changes
- 📈 **Trade Entries**: When new positions are opened
- 📉 **Trade Exits**: When positions are closed (with reason & P&L)

## Example Log Messages

```
📊 Status Update
DEFCON: 5/5 | Signal: 2.0/100 | VIX: 20.6 | Yield: 4.09%
Holdings: GOOGL, NVDA, MSFT
```

```
🚨 DEFCON Changed: 5 → 2
Signal Score: 85.3/100
```

```
📈 Trade Entry
Assets: GOOGL, NVDA, MSFT | Size: $10,000 | DEFCON: 2
```

```
📉 Trade Exit
Asset: GOOGL | Reason: defcon_revert | P&L: +2.3%
```

## Benefits

- **Silent**: No @channel notifications
- **Historical**: Complete audit trail of all system activity
- **Searchable**: Easy to query Slack history for specific events
- **Continuous**: Every monitoring cycle logged automatically
