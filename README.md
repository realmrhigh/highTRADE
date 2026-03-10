# HighTrade Autonomous Trading System

**Location**: `/Users/stantonhigh/Documents/hightrade`  
**User**: stantonhigh (admin)  
**Status**: Production Ready ✅  
**Version**: 1.1.0 (Enhanced)

## Quick Start

```bash
cd ~/Documents/hightrade

# Check system status
python3 hightrade_cmd.py /status

# Run configuration validator
python3 config_validator.py

# View live logs
tail -f trading_data/logs/orchestrator_output.log

# Test Slack integration
# Send "status" in any Slack channel the bot has joined, or DM the bot directly
```

## What's New in v1.1.0 🎉

### Phase 1 Enhancements (Complete)

✅ **Configuration Validation** - Startup health checks for all APIs and services  
✅ **News Deduplication** - Content similarity detection prevents duplicate news inflation  
✅ **Rate Limiting** - Exponential backoff for API calls (zero data loss)  
✅ **Enhanced Exit Strategies** - 5 exit strategies vs. previous 2:
- Trailing stops (2% from peak)
- DEFCON reversion exits
- Time-based exits (72hr max)
- Profit target (+5%)
- Stop loss (-3%)

📖 See `docs/ENHANCEMENTS_SUMMARY.md` for detailed information

## System Architecture

```
~/Documents/hightrade/
├── Core System
│   ├── hightrade_orchestrator.py    # Main monitoring loop
│   ├── slack_bot.py                  # Slack command listener
│   ├── mcp_server.py                 # Claude Desktop integration
│   ├── monitoring.py                 # DEFCON & signal scoring
│   ├── broker_agent.py               # Trade decision logic
│   └── logging_config.py             # Unified logging system
│
├── News & Signals (Enhanced)
│   ├── news_aggregator.py            # Multi-source news (with deduplication)
│   ├── news_deduplicator.py          # TF-IDF similarity detection
│   ├── news_sentiment.py             # Sentiment analysis
│   └── news_signals.py               # News-based signals
│
├── Trading (Enhanced)
│   ├── paper_trading.py              # Simulated trading (with enhanced exits)
│   ├── exit_strategies.py            # 5 exit strategies with priorities
│   └── live_trading.py               # Live trading (future)
│
├── Infrastructure (New)
│   ├── config_validator.py           # Startup health checks
│   ├── rate_limiter.py               # API rate limiting
│   └── alerts.py                     # Slack/email/SMS alerts
│
├── Data & Config
│   ├── trading_data/
│   │   ├── trading_history.db        # SQLite database
│   │   ├── alert_config.json         # Slack/alert settings
│   │   ├── logs/                     # All system logs
│   │   └── commands/                 # Command IPC files
│   └── docs/
│       ├── ENHANCEMENTS_SUMMARY.md   # Phase 1 enhancements
│       ├── MIGRATION_COMPLETE.md     # Migration history
│       └── README.md                 # This file
└── Tests & Utilities
    ├── create_database.py
    ├── load_sample_data.py
    ├── test_paper_trading.py
    └── test_claude_feedback.py
```

## Running Services

All three services run via launchd and auto-restart on crash. See `KEEPALIVE_SETUP.md` for details.

```bash
./start_with_launchd.sh   # Start all three
./stop_launchd.sh         # Stop all three
launchctl list | grep hightrade   # Check status
```

### Dashboard (launchd)
- **Label**: `com.hightrade2.dashboard`
- **URL**: http://localhost:5055 · http://192.168.0.233:5055
- **Log**: `logs/dashboard_srv.log`

### Orchestrator (launchd)
- **Label**: `com.hightrade2.orchestrator`
- **Interval**: Every 15 minutes
- **Log**: `logs/orchestrator_srv.log`

### Slack Bot (launchd)
- **Label**: `com.hightrade.slackbot`
- **Function**: Listens for commands in any joined Slack channel, group DM, or direct message
- **Log**: `logs/slack_bot.log`

### MCP Server (Claude Desktop)
- **Path**: `/Users/stantonhigh/Documents/hightrade/mcp_server.py`
- **Config**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Status**: Managed by Claude Desktop
- **Restart**: Restart Claude Desktop to reload

## Commands

### CLI Commands
```bash
cd ~/Documents/hightrade

# System status
python3 hightrade_cmd.py /status

# Portfolio
python3 hightrade_cmd.py /portfolio

# DEFCON details
python3 hightrade_cmd.py /defcon

# Help
python3 hightrade_cmd.py /help

# Run health check
python3 config_validator.py
```

### Slack Commands
Send these in any Slack channel the bot has joined, any group DM, or directly to the bot:
- `status` - System status
- `portfolio` - View positions
- `defcon` - DEFCON level details
- `help` - All commands
- `hold` - Pause trading
- `yes` - Approve pending trade
- `no` - Reject pending trade

You can also mention the bot, for example: `@HighTrade status`

### Service Management
```bash
# Start / stop all
./start_with_launchd.sh
./stop_launchd.sh

# Check status
launchctl list | grep hightrade

# Restart individual services
launchctl kickstart -k gui/$(id -u)/com.hightrade2.orchestrator
launchctl kickstart -k gui/$(id -u)/com.hightrade2.dashboard
launchctl kickstart -k gui/$(id -u)/com.hightrade.slackbot

# View logs
tail -f logs/orchestrator_srv.log
tail -f logs/dashboard_srv.log
tail -f logs/slack_bot.log
```

## Monitoring

### View Logs
```bash
# Live logs
tail -f logs/orchestrator_srv.log
tail -f logs/dashboard_srv.log
tail -f logs/slack_bot.log

# Daily rotating orchestrator log
tail -f logs/hightrade_$(date +%Y%m%d).log

# Watch for rate limit events
grep -i "rate limit" logs/orchestrator_srv.log

# See deduplication stats
grep -i "deduplication" logs/orchestrator_srv.log

# Check exit strategy triggers
grep -E "TRAILING STOP|TIME LIMIT|DEFCON REVERT" logs/orchestrator_srv.log
```

### Check Database
```bash
cd ~/Documents/hightrade
python3 -c "
import sqlite3
conn = sqlite3.connect('trading_data/trading_history.db')
cursor = conn.cursor()
cursor.execute('SELECT COUNT(*) FROM trade_records')
print(f'Total trades: {cursor.fetchone()[0]}')
cursor.execute('SELECT COUNT(*) FROM signal_monitoring')
print(f'Signal records: {cursor.fetchone()[0]}')
cursor.execute('SELECT COUNT(*) FROM news_signals')
print(f'News signals: {cursor.fetchone()[0]}')
"
```

## Configuration

### Slack Integration
**Config File**: `trading_data/alert_config.json`

Contains:
- Webhook URL for posting alerts
- Bot token for listening to commands
- App token for Socket Mode (optional)
- Alert thresholds (DEFCON 2 & 1 enabled)

### News Deduplication
**Config**: In `news_config.json` (optional)
```json
{
  "deduplication": {
    "similarity_threshold": 0.6
  }
}
```
- Default threshold: 0.6 (60% similarity = duplicate)
- Lower = stricter (fewer duplicates caught)
- Higher = looser (more false positives)

### Rate Limiting
**Built-in Defaults**:
- Alpha Vantage: 5 requests/min, 12s min delay
- Reddit: 60 requests/min, 1s min delay
- Exponential backoff: 2^failures seconds (max 5min)

### Exit Strategies
**Built-in Configuration**:
- Profit Target: +5%
- Stop Loss: -3%
- Trailing Stop: 2% from peak
- Max Hold Time: 72 hours
- Min Hold Time: 1 hour

### Database
**File**: `trading_data/trading_history.db`  
**Size**: ~220 KB  
**Tables** (14 total):
- `trade_records` - All paper trades
- `signal_monitoring` - DEFCON history
- `defcon_history` - Historical DEFCON levels
- `crisis_events` - Crisis catalog
- `news_signals` - News analysis
- `claude_analysis` - Claude feedback
- `market_data`, `market_signals`, etc.

## Dependencies

Installed via `pip3 install --user`:
- `requests` - HTTP requests for APIs
- `slack_sdk` - Slack integration
- `yfinance` - Market data
- `feedparser` - RSS feed parsing
- Standard library: `sqlite3`, `logging`, `pathlib`, etc.

## Safety Features

✅ **Paper Trading Only** - No real money at risk  
✅ **Manual Approval Required** - All trades need `/yes` command  
✅ **Conservative Thresholds** - Only trades at DEFCON 2/1  
✅ **Position Limits** - Max 60% portfolio exposure  
✅ **5 Exit Strategies** - Multi-layer risk management  
✅ **Slack Alerts** - Real-time notifications  
✅ **Complete Audit Trail** - Every decision logged  
✅ **Config Validation** - Catches issues on startup  
✅ **Rate Limiting** - Prevents API data loss  
✅ **News Deduplication** - Accurate sentiment scores

## Current Status

Run `python3 hightrade_cmd.py /status` to see:
- DEFCON Level (1-5)
- Signal Score (0-100)
- Market Data (VIX, Bond Yields, S&P 500)
- Open Positions (with exit strategy tracking)
- Pending Trades
- News Score (with deduplication)

## MCP Tools (Claude Desktop)

After restarting Claude Desktop, these tools are available:
- `get_system_status` - Current system state
- `get_recent_signals` - Market signals
- `get_recent_news` - News analysis (deduplicated)
- `submit_claude_analysis` - Enhanced news review
- `get_article_details` - Full article data
- `get_system_architecture` - System info

## Testing & Validation

### Test New Features
```bash
cd ~/Documents/hightrade

# Test config validator
python3 config_validator.py

# Test news deduplicator
python3 news_deduplicator.py

# Test rate limiter
python3 rate_limiter.py

# Test exit strategies
python3 exit_strategies.py
```

### Integration Tests
```bash
# Test paper trading with enhanced exits
python3 test_paper_trading.py

# Test Claude feedback system
python3 test_claude_feedback.py
```

## Troubleshooting

### Orchestrator Not Running
```bash
launchctl list | grep hightrade   # check exit code
tail -f logs/orchestrator_srv.log
launchctl kickstart -k gui/$(id -u)/com.hightrade2.orchestrator
# If exit code 78 persists after kickstart, see KEEPALIVE_SETUP.md (label poisoning)
```

### Slack Bot Not Responding
```bash
launchctl list | grep hightrade   # check exit code
tail -f logs/slack_bot.log
launchctl kickstart -k gui/$(id -u)/com.hightrade.slackbot
```

### Dashboard Not Loading
```bash
lsof -i :5055   # check if port is bound
tail -f logs/dashboard_srv.log
launchctl kickstart -k gui/$(id -u)/com.hightrade2.dashboard
```

### MCP Server Not Working
1. Check config: `cat ~/Library/Application\ Support/Claude/claude_desktop_config.json`
2. Verify path: `/Users/stantonhigh/Documents/hightrade/mcp_server.py`
3. Restart Claude Desktop
4. Check Claude Desktop logs

### Rate Limit Errors
```bash
# Check rate limiter stats in logs
grep "Rate limit" trading_data/logs/orchestrator_output.log

# If seeing many backoffs, API keys may be rate limited
# Wait for backoff period (shows in logs)
```

### News Deduplication Not Working
```bash
# Check if deduplicator is loaded
grep "deduplication enabled" trading_data/logs/orchestrator_output.log

# See deduplication stats
grep "Deduplication:" trading_data/logs/orchestrator_output.log
```

## Performance Metrics

### Before v1.1.0
- News duplicates counted multiple times
- Rate limit errors caused data loss  
- Only 2 exit strategies (profit target + stop loss)
- No health checks on startup
- No protection for prolonged holds

### After v1.1.0
- Accurate news scores (duplicates removed)
- Zero data loss from rate limits
- 5 exit strategies with priority ordering
- Startup validation catches config issues
- Automatic exits after 72 hours
- DEFCON reversion exits
- Trailing stops protect profits

## Documentation

- `docs/ENHANCEMENTS_SUMMARY.md` - Phase 1 enhancements detailed
- `docs/MIGRATION_COMPLETE.md` - System migration history
- `README.md` - This file (main documentation)

## Roadmap

### Phase 1 (Complete) ✅
- Configuration validation
- News deduplication
- Rate limiting
- Enhanced exit strategies

### Phase 2 (Planned) 📋
- Backtesting framework
- Performance analytics dashboard
- Multi-timeframe analysis
- Advanced position sizing

---

**System Status**: ✅ Production Ready (Enhanced)  
**Last Updated**: 2026-02-14  
**Version**: 1.1.0  
**Enhancements**: 4 of 5 complete (80%)
