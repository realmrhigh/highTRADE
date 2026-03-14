#!/bin/bash
# Single-run watchdog check (for cron)
# Checks if processes are running and restarts if needed

HIGHTRADE_DIR=/Users/stantonhigh/Documents/hightrade
PYTHON=/opt/homebrew/bin/python3.11
cd "$HIGHTRADE_DIR"

# Check orchestrator
if ! pgrep -f "hightrade_orchestrator.py" > /dev/null; then
    echo "[$(date)] Orchestrator not running - restarting..."
    nohup "$PYTHON" hightrade_orchestrator.py continuous 15 \
        >> logs/orchestrator.log \
        2>&1 &
    disown $!
    echo "[$(date)] Orchestrator restarted (PID: $!)"
fi

# Check Slack bot
if ! pgrep -f "slack_bot.py" > /dev/null; then
    echo "[$(date)] Slack bot not running - restarting..."
    nohup "$PYTHON" slack_bot.py \
        >> trading_data/logs/slack_bot_output.log \
        2>&1 &
    disown $!
    echo "[$(date)] Slack bot restarted (PID: $!)"
fi

# Check Dashboard server (port 5055)
if ! pgrep -f "dashboard.py.*--serve\|dashboard\.py -s" > /dev/null; then
    echo "[$(date)] Dashboard not running - restarting..."
    # Kill anything still holding port 5055 to avoid bind conflict
    STALE=$(lsof -ti :5055 2>/dev/null)
    if [ -n "$STALE" ]; then
        echo "[$(date)] Killing stale process on port 5055: $STALE"
        kill -9 $STALE 2>/dev/null
        sleep 1
    fi
    nohup "$PYTHON" dashboard.py --serve \
        >> logs/dashboard.log \
        2>&1 &
    disown $!
    echo "[$(date)] Dashboard restarted (PID: $!)"
fi

# Log status if all healthy
if pgrep -f "hightrade_orchestrator.py" > /dev/null \
   && pgrep -f "slack_bot.py" > /dev/null \
   && pgrep -f "dashboard.py" > /dev/null; then
    echo "[$(date)] ✓ All processes healthy"
fi
