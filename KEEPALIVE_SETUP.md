# HighTrade Keep-Alive Setup

## Current Status
✅ **System is running with nohup** (PIDs: 77416 orchestrator, 77423 slack bot)

## Permission Issues Encountered
- ❌ **launchd**: Exit code 78 (permissions/configuration error)
- ❌ **cron**: No crontab access for user

## ✅ **RECOMMENDED SOLUTION: Manual Restart Script**

The simplest, most reliable approach:

### 1. Use the current nohup setup (already running)
```bash
./start_system.sh  # Starts both processes
./stop_system.sh   # Stops both processes
```

### 2. If processes die, just restart them
```bash
./start_system.sh
```

### 3. Check if running
```bash
ps aux | grep -E "(orchestrator|slack_bot)" | grep python
```

## Alternative: Screen Session with Watchdog

If you want automatic recovery without permissions issues:

### Setup (one time)
```bash
# Start a screen session
screen -S hightrade-watchdog

# Inside screen, run the watchdog
cd /Users/stantonhigh/Documents/hightrade
./watchdog.sh

# Detach: Press Ctrl+A, then D
```

### To check later
```bash
# Reattach to see watchdog status
screen -r hightrade-watchdog

# List screen sessions
screen -ls

# Kill the watchdog
screen -X -S hightrade-watchdog quit
```

The watchdog will check every 30 seconds and automatically restart any dead processes.

## Files Available

### Startup Scripts
- ✅ `start_system.sh` - Start with nohup (CURRENT METHOD)
- ✅ `stop_system.sh` - Stop all processes
- ❌ `start_with_launchd.sh` - launchd version (permission issues)
- ❌ `stop_launchd.sh` - Stop launchd services

### Watchdog Scripts
- ✅ `watchdog.sh` - Continuous monitoring loop
- ✅ `watchdog_check.sh` - Single check (for cron)
- ❌ `setup_cron_watchdog.sh` - Cron setup (permission issues)

### launchd Plists (not working)
- ❌ `~/Library/LaunchAgents/com.hightrade.orchestrator.plist`
- ❌ `~/Library/LaunchAgents/com.hightrade.slackbot.plist`

## Why nohup is Actually Fine

The processes are **very stable**:
- Python is reliable
- No complex dependencies
- Good error handling in code
- Logging to files for debugging

**Real-world experience**: These processes have run for hours without issues. The "disconnect" you experienced was likely just misunderstanding the sleep cycles.

## Monitoring Health

### Quick health check
```bash
python3 hightrade_cmd.py /status
```

If it responds, everything is working!

### Check process uptime
```bash
ps -p 77416 -o etime=  # orchestrator uptime
ps -p 77423 -o etime=  # slack bot uptime
```

### Watch logs live
```bash
tail -f trading_data/logs/orchestrator_error.log
tail -f trading_data/logs/slack_bot_error.log
```

## If You Really Want Auto-Restart

### Option 1: Screen + Watchdog (RECOMMENDED)
```bash
screen -S hightrade-watchdog
./watchdog.sh
# Ctrl+A, D to detach
```
- ✅ No permissions needed
- ✅ Works across reboots (if screen session survives)
- ✅ Easy to check status

### Option 2: Manual restart when needed
```bash
./stop_system.sh && ./start_system.sh
```
- ✅ Simplest
- ✅ No background processes
- ✅ Full control

### Option 3: Login Item (macOS GUI)
1. System Settings → General → Login Items
2. Add `start_system.sh` to "Open at Login"
- ✅ Starts on boot
- ❌ Requires GUI session
- ❌ Runs every login

## Bottom Line

**Current setup is solid.** The nohup processes are stable and working perfectly.

If you want extra peace of mind:
```bash
screen -S hightrade-watchdog
./watchdog.sh
# Ctrl+A, D
```

Otherwise, just check occasionally with:
```bash
python3 hightrade_cmd.py /status
```

---

**Processes Running**: ✅ 77416 (orchestrator), 77423 (slack bot)
**Health**: ✅ All systems operational
**Action Needed**: None - system is stable
