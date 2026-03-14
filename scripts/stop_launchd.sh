#!/bin/bash
# Stop HighTrade launchd services

YELLOW='\033[1;33m'
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${YELLOW}========================================${NC}"
echo -e "${YELLOW}  HighTrade System - launchd Shutdown${NC}"
echo -e "${YELLOW}========================================${NC}"

echo -e "\n${YELLOW}Stopping launchd jobs...${NC}"
launchctl bootout gui/$(id -u)/com.hightrade2.dashboard 2>/dev/null   && echo "  ✅ Dashboard stopped"    || echo "  ⚠️  Dashboard not running"
launchctl bootout gui/$(id -u)/com.hightrade2.orchestrator 2>/dev/null && echo "  ✅ Orchestrator stopped" || echo "  ⚠️  Orchestrator not running"
launchctl bootout gui/$(id -u)/com.hightrade.slackbot 2>/dev/null      && echo "  ✅ Slack bot stopped"    || echo "  ⚠️  Slack bot not running"

sleep 2

# Force-kill any orphans
echo -e "\n${GREEN}Verification:${NC}"
LEFTOVER=false
pgrep -f "hightrade_orchestrator.py" > /dev/null && { echo "  ⚠️  Orphaned orchestrator — force killing..."; pkill -9 -f "hightrade_orchestrator.py" 2>/dev/null; LEFTOVER=true; }
pgrep -f "slack_bot.py" > /dev/null              && { echo "  ⚠️  Orphaned slack_bot — force killing..."; pkill -9 -f "slack_bot.py" 2>/dev/null; LEFTOVER=true; }
pgrep -f "dashboard.py.*serve" > /dev/null       && { echo "  ⚠️  Orphaned dashboard — force killing..."; pkill -9 -f "dashboard.py" 2>/dev/null; LEFTOVER=true; }

if [ "$LEFTOVER" = false ]; then
    echo -e "  ${GREEN}All processes stopped cleanly${NC}"
fi

echo -e "\n${YELLOW}========================================${NC}"
