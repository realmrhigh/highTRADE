#!/bin/bash
# HighTrade System Startup with launchd (keeps processes alive automatically)

set -e

cd /Users/stantonhigh/Documents/hightrade

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  HighTrade System - launchd Startup${NC}"
echo -e "${GREEN}========================================${NC}"

# Stop any manually started processes first
echo -e "\n${YELLOW}Stopping any manual processes...${NC}"
pkill -f "hightrade_orchestrator.py" 2>/dev/null || true
pkill -f "slack_bot.py" 2>/dev/null || true
sleep 2

# Unload existing launchd jobs if running
echo -e "\n${YELLOW}Unloading existing launchd jobs...${NC}"
launchctl unload ~/Library/LaunchAgents/com.hightrade.orchestrator.plist 2>/dev/null || true
launchctl unload ~/Library/LaunchAgents/com.hightrade.slackbot.plist 2>/dev/null || true
sleep 1

# Load and start launchd jobs
echo -e "\n${GREEN}Loading launchd jobs...${NC}"
launchctl load ~/Library/LaunchAgents/com.hightrade.orchestrator.plist
echo "  ✅ Orchestrator job loaded"

launchctl load ~/Library/LaunchAgents/com.hightrade.slackbot.plist
echo "  ✅ Slack bot job loaded"

# Wait for processes to start
sleep 5

# Verify
echo -e "\n${GREEN}========================================${NC}"
echo -e "${GREEN}  System Status${NC}"
echo -e "${GREEN}========================================${NC}"

if pgrep -f "hightrade_orchestrator.py" > /dev/null; then
    PID=$(pgrep -f "hightrade_orchestrator.py")
    echo -e "  Orchestrator: ${GREEN}RUNNING${NC} (PID: $PID)"
else
    echo -e "  Orchestrator: ${RED}NOT RUNNING${NC}"
    echo "  Check: launchctl list | grep orchestrator"
fi

if pgrep -f "slack_bot.py" > /dev/null; then
    PID=$(pgrep -f "slack_bot.py")
    echo -e "  Slack Bot:    ${GREEN}RUNNING${NC} (PID: $PID)"
else
    echo -e "  Slack Bot:    ${RED}NOT RUNNING${NC}"
    echo "  Check: launchctl list | grep slackbot"
fi

echo -e "\n${YELLOW}launchd will automatically restart these if they crash!${NC}"

echo -e "\n${YELLOW}Logs:${NC}"
echo "  Orchestrator: tail -f trading_data/logs/orchestrator_error.log"
echo "  Slack Bot:    tail -f trading_data/logs/slack_bot_error.log"

echo -e "\n${YELLOW}Commands:${NC}"
echo "  Status:  python3 hightrade_cmd.py /status"
echo "  Stop:    ./stop_launchd.sh"
echo "  Restart: launchctl kickstart -k gui/\$(id -u)/com.hightrade.orchestrator"

echo -e "\n${GREEN}========================================${NC}"
