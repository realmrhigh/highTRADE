#!/bin/bash
# Simple FD watchdog: if orchestrator exceeds FD threshold, restart it
PIDFILE="/Users/traderbot/Documents/highTRADE/hightrade_orchestrator.pid"
ORCH_CMD="/usr/local/Cellar/python@3.14/3.14.3_1/Frameworks/Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/Python /Users/traderbot/Documents/highTRADE/hightrade_orchestrator.py continuous"
THRESHOLD=300
while true; do
  pid=$(pgrep -f "hightrade_orchestrator.py")
  if [ -n "$pid" ]; then
    fdcount=$(lsof -p $pid 2>/dev/null | wc -l)
    if [ "$fdcount" -gt "$THRESHOLD" ]; then
      echo "$(date) - FD count $fdcount > $THRESHOLD. Restarting orchestrator" >> /Users/traderbot/Documents/highTRADE/logs/watchdog.log
      kill $pid
      sleep 1
      nohup $ORCH_CMD >> /Users/traderbot/Documents/highTRADE/logs/orchestrator_run.log 2>&1 &
      sleep 5
    fi
  fi
  sleep 30
done
