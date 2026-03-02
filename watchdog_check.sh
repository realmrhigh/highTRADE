#!/bin/bash
# Single-run watchdog check (for cron)
# Checks if processes are running and restarts if needed

cd /Users/stantonhigh/Documents/hightrade

# Check orchestrator
if ! pgrep -f "hightrade_orchestrator.py" > /dev/null; then
    echo "[$(date)] Orchestrator not running - restarting..."
    nohup /opt/homebrew/bin/python3.11 hightrade_orchestrator.py continuous 15 \
        >> logs/orchestrator.log \
        2>&1 &
    echo "[$(date)] Orchestrator restarted (PID: $!)"
fi

# Check Slack bot
if ! pgrep -f "slack_bot.py" > /dev/null; then
    echo "[$(date)] Slack bot not running - restarting..."
    nohup /opt/homebrew/bin/python3.11 slack_bot.py \
        >> trading_data/logs/slack_bot_output.log \
        2>&1 &
    echo "[$(date)] Slack bot restarted (PID: $!)"
fi

# Log status if both running (quiet success)
if pgrep -f "hightrade_orchestrator.py" > /dev/null && pgrep -f "slack_bot.py" > /dev/null; then
    echo "[$(date)] ✓ Both processes healthy"
fi
