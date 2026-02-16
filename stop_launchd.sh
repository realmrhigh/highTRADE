#!/bin/bash
# Stop HighTrade launchd services

# Colors
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
NC='\033[0m'

echo -e "${YELLOW}========================================${NC}"
echo -e "${YELLOW}  HighTrade System - launchd Shutdown${NC}"
echo -e "${YELLOW}========================================${NC}"

echo -e "\n${YELLOW}Unloading launchd jobs...${NC}"
launchctl unload ~/Library/LaunchAgents/com.hightrade.orchestrator.plist 2>/dev/null && echo "  ✅ Orchestrator unloaded" || echo "  ⚠️  Orchestrator not loaded"
launchctl unload ~/Library/LaunchAgents/com.hightrade.slackbot.plist 2>/dev/null && echo "  ✅ Slack bot unloaded" || echo "  ⚠️  Slack bot not loaded"

sleep 2

# Verify stopped
echo -e "\n${GREEN}Verification:${NC}"
if ! pgrep -f "hightrade_orchestrator.py" > /dev/null && ! pgrep -f "slack_bot.py" > /dev/null; then
    echo -e "  ${GREEN}All processes stopped successfully${NC}"
else
    echo -e "  ⚠️  Some processes still running - force killing..."
    pkill -9 -f "hightrade_orchestrator.py" 2>/dev/null || true
    pkill -9 -f "slack_bot.py" 2>/dev/null || true
fi

echo -e "\n${YELLOW}========================================${NC}"
