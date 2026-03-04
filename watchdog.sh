#!/bin/bash
# HighTrade Watchdog - Keeps all processes alive
# Monitors: orchestrator, slack bot, dashboard (port 5055)
# Run via: nohup bash watchdog.sh >> trading_data/logs/watchdog.log 2>&1 &

HIGHTRADE_DIR=/Users/stantonhigh/Documents/hightrade
PYTHON=/opt/homebrew/bin/python3.11
LOG_DIR="$HIGHTRADE_DIR/trading_data/logs"

cd "$HIGHTRADE_DIR"

echo "[$(date)] 🐕 Watchdog started (PID: $$)"

while true; do

    # ── Orchestrator ─────────────────────────────────────────────────────────
    if ! pgrep -f "hightrade_orchestrator.py" > /dev/null; then
        echo "[$(date)] Orchestrator died - restarting..."
        nohup "$PYTHON" hightrade_orchestrator.py continuous 15 \
            >> "$LOG_DIR/orchestrator.log" 2>&1 &
        disown $!
        echo "[$(date)] Orchestrator restarted (PID: $!)"
        sleep 5
    fi

    # ── Slack Bot ────────────────────────────────────────────────────────────
    if ! pgrep -f "slack_bot.py" > /dev/null; then
        echo "[$(date)] Slack bot died - restarting..."
        nohup "$PYTHON" slack_bot.py \
            >> "$LOG_DIR/slack_bot.log" 2>&1 &
        disown $!
        echo "[$(date)] Slack bot restarted (PID: $!)"
        sleep 5
    fi

    # ── Dashboard (port 5055) ────────────────────────────────────────────────
    if ! pgrep -f "dashboard.py.*--serve\|dashboard\.py -s" > /dev/null; then
        echo "[$(date)] Dashboard died - restarting..."
        # Kill anything holding port 5055 to avoid bind conflict
        STALE=$(lsof -ti :5055 2>/dev/null)
        if [ -n "$STALE" ]; then
            echo "[$(date)] Killing stale process on port 5055: $STALE"
            kill -9 $STALE 2>/dev/null
            sleep 1
        fi
        nohup "$PYTHON" dashboard.py --serve \
            >> "$LOG_DIR/dashboard.log" 2>&1 &
        disown $!
        echo "[$(date)] Dashboard restarted (PID: $!)"
        sleep 5
    fi

    # ── Health summary every 10 min (20 × 30s) ──────────────────────────────
    TICK=$(( ${TICK:-0} + 1 ))
    if (( TICK % 20 == 0 )); then
        ORC=$(pgrep -f "hightrade_orchestrator.py" | head -1)
        SLK=$(pgrep -f "slack_bot.py" | head -1)
        DSH=$(pgrep -f "dashboard.py" | head -1)
        echo "[$(date)] ✓ Health: orchestrator=$ORC slack=$SLK dashboard=$DSH"
    fi

    sleep 30
done
