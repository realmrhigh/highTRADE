# HighTrade System Status

## ✅ FIXED: Path Configuration Bug

### Issue
The system was losing connection to the Slack choreographer due to **incorrect file paths**. Multiple core files were using `Path.home() / 'trading_data'` instead of `SCRIPT_DIR / 'trading_data'`, causing them to look for files in the wrong directory.

### Files Fixed
1. **alerts.py** - Alert system (Slack, Email, SMS)
2. **monitoring.py** - Market monitoring and signal detection
3. **broker_agent.py** - Autonomous trading agent
4. **dashboard.py** - Dashboard generation (3 locations)

All files now use:
```python
SCRIPT_DIR = Path(__file__).parent.resolve()
DB_PATH = SCRIPT_DIR / 'trading_data' / 'trading_history.db'
CONFIG_PATH = SCRIPT_DIR / 'trading_data' / 'alert_config.json'
```

## ✅ System Configuration

### Slack Bot Configuration
- **Channel ID**: C0AE47ZLJCQ (#all-hightrade)
- **Bot User**: hightrade (U0ACUG36PHD)
- **Polling Mode**: Active (every 2 seconds)
- **Webhook**: Configured and tested ✅

### Orchestrator Configuration
- **Monitoring Interval**: 15 minutes
- **Broker Mode**: DISABLED (manual approval required)
- **DEFCON Level**: 5/5 (PEACETIME)
- **Signal Score**: 2.0/100

## 🚀 System Management

### Start System
```bash
./start_system.sh
```

### Stop System
```bash
./stop_system.sh
```

### Check Status
```bash
python3 hightrade_cmd.py /status
```

### View Logs
```bash
# Orchestrator logs
tail -f trading_data/logs/orchestrator_error.log

# Slack bot logs
tail -f trading_data/logs/slack_bot_error.log

# Main application log
tail -f trading_data/logs/hightrade_$(date +%Y%m%d).log
```

## 📊 Current System Status

### Running Processes
- ✅ Orchestrator: RUNNING
- ✅ Slack Bot: RUNNING

### Open Positions
- GOOGL: $155.00 (+0.0%)
- NVDA: $920.00 (+0.0%)
- MSFT: $385.00 (+0.0%)

### Market Conditions
- Bond Yield (10Y): 4.09%
- VIX: 20.6
- DEFCON: 5/5

## 🔧 Available Slash Commands

### Via Python CLI
```bash
python3 hightrade_cmd.py /status    # System status
python3 hightrade_cmd.py /portfolio # Portfolio summary
python3 hightrade_cmd.py /defcon    # DEFCON status
python3 hightrade_cmd.py /hold      # Pause trading
python3 hightrade_cmd.py /start     # Resume trading
python3 hightrade_cmd.py /help      # Full command list
```

### Via Slack (in #all-hightrade channel)
Type any command without the `python3 hightrade_cmd.py` prefix:
- `status` or `/status`
- `portfolio` or `/portfolio`
- `hold` or `/hold`
- etc.

The Slack bot will respond in a thread to your message.

## ⚠️ Important Notes

### NO /user/ Processes Found
There were **no `/user/` processes** to kill. The issue was purely a path configuration bug.

### Path Configuration is Critical
All modules must use `SCRIPT_DIR / 'trading_data'` to ensure they're looking in the correct project directory (`/Users/stantonhigh/Documents/hightrade/trading_data`).

### Slack Connection Stability
The Slack bot now properly connects because:
1. Fixed paths in `alerts.py`
2. Added `channel_id` to `alert_config.json`
3. Proper error logging enabled

## 🐛 Debugging Tips

If connection is lost again:
1. Check process status: `ps aux | grep -E "(slack_bot|orchestrator)"`
2. Check logs: `tail -f trading_data/logs/*.log`
3. Verify paths are correct in all modules
4. Test Slack connection: `python3 alerts.py test`
5. Restart system: `./stop_system.sh && ./start_system.sh`

## 📝 System Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   User (Slack / CLI)                    │
└────────────────────┬────────────────────────────────────┘
                     │
        ┌────────────┴────────────┐
        │                         │
┌───────▼────────┐       ┌────────▼─────────┐
│  Slack Bot     │       │  CLI Commands    │
│  (slack_bot.py)│       │  (hightrade_cmd) │
└───────┬────────┘       └────────┬─────────┘
        │                         │
        └────────────┬────────────┘
                     │
          ┌──────────▼──────────────┐
          │  Command Processor      │
          │  (hightrade_cmd.py)     │
          └──────────┬──────────────┘
                     │
          ┌──────────▼──────────────┐
          │  Orchestrator           │
          │  (hightrade_orchestrat…)│
          └──────────┬──────────────┘
                     │
        ┌────────────┼────────────┐
        │            │            │
   ┌────▼───┐   ┌───▼────┐   ┌──▼─────┐
   │Monitor │   │Broker  │   │Alerts  │
   │        │   │Agent   │   │        │
   └────────┘   └────────┘   └────────┘
        │            │            │
        └────────────┴────────────┘
                     │
          ┌──────────▼──────────────┐
          │  Database & Config      │
          │  (trading_data/)        │
          └─────────────────────────┘
```

---
**Last Updated**: 2026-02-15 10:35 PST
**Status**: ✅ OPERATIONAL
**Issues**: None
