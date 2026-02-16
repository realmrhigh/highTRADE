#!/bin/bash
# HighTrade Watchdog - Keeps processes alive
# Run this in a screen/tmux session or as a cron job

cd /Users/stantonhigh/Documents/hightrade

while true; do
    # Check orchestrator
    if ! pgrep -f "hightrade_orchestrator.py" > /dev/null; then
        echo "[$(date)] Orchestrator died - restarting..."
        nohup python3 hightrade_orchestrator.py continuous 15 \
            > trading_data/logs/orchestrator_output.log \
            2> trading_data/logs/orchestrator_error.log &
        sleep 5
    fi

    # Check Slack bot
    if ! pgrep -f "slack_bot.py" > /dev/null; then
        echo "[$(date)] Slack bot died - restarting..."
        nohup python3 slack_bot.py \
            > trading_data/logs/slack_bot_output.log \
            2> trading_data/logs/slack_bot_error.log &
        sleep 5
    fi

    # Check every 30 seconds
    sleep 30
done
