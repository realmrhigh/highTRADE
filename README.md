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
# Send "status" in #all-hightrade Slack channel
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

### Orchestrator (LaunchD)
- **Service**: `com.hightrade.orchestrator`
- **Status**: Auto-starts on boot, auto-restarts on crash
- **Interval**: Monitoring every 15 minutes
- **Features**:
  - ✅ Config validation on startup
  - ✅ News deduplication active
  - ✅ Rate limiting enabled
- **Logs**: `trading_data/logs/orchestrator_*.log`

### Slack Bot (Background Process)
- **Process**: `slack_bot.py`
- **Status**: Running (PID in `trading_data/slack_bot.pid`)
- **Function**: Listens for commands in #all-hightrade
- **Logs**: `trading_data/logs/slack_bot_*.log`

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
Send these in #all-hightrade channel:
- `status` - System status
- `portfolio` - View positions
- `defcon` - DEFCON level details
- `help` - All commands
- `hold` - Pause trading
- `yes` - Approve pending trade
- `no` - Reject pending trade

### Service Management
```bash
# Check status
sudo launchctl list | grep hightrade
ps aux | grep -E "hightrade_orchestrator|slack_bot" | grep -v grep

# Restart orchestrator (loads new enhancements)
sudo launchctl stop com.hightrade.orchestrator

# Restart Slack bot
pkill -f slack_bot.py
cd ~/Documents/hightrade
nohup python3 slack_bot.py > trading_data/logs/slack_bot_output.log 2>&1 &
echo $! > trading_data/slack_bot.pid
```

## Monitoring

### View Logs
```bash
# Real-time all logs
tail -f ~/Documents/hightrade/trading_data/logs/orchestrator_output.log

# Errors only
tail -f ~/Documents/hightrade/trading_data/logs/*error.log

# Slack bot
tail -f ~/Documents/hightrade/trading_data/logs/slack_bot_output.log

# Watch for rate limit events
grep -i "rate limit" trading_data/logs/orchestrator_output.log

# See deduplication stats
grep -i "deduplication" trading_data/logs/orchestrator_output.log

# Check exit strategy triggers
grep -E "TRAILING STOP|TIME LIMIT|DEFCON REVERT" trading_data/logs/orchestrator_output.log
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
sudo launchctl load /Library/LaunchDaemons/com.hightrade.orchestrator.plist
tail -f ~/Documents/hightrade/trading_data/logs/orchestrator_error.log
```

### Slack Bot Not Responding
```bash
# Check if running
ps aux | grep slack_bot.py

# Restart
pkill -f slack_bot.py
cd ~/Documents/hightrade
nohup python3 slack_bot.py > trading_data/logs/slack_bot_output.log 2>&1 &
echo $! > trading_data/slack_bot.pid
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
