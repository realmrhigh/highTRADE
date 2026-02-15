#!/bin/bash
# HighTrade System Shutdown Script
# Gracefully stops both the orchestrator and Slack bot

cd /Users/stantonhigh/Documents/hightrade

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${YELLOW}========================================${NC}"
echo -e "${YELLOW}  HighTrade System Shutdown${NC}"
echo -e "${YELLOW}========================================${NC}"

echo -e "\n${YELLOW}Stopping orchestrator...${NC}"
if pgrep -f "hightrade_orchestrator.py" > /dev/null; then
    pkill -f "hightrade_orchestrator.py"
    echo -e "  ${GREEN}Orchestrator stopped${NC}"
else
    echo -e "  ${RED}Orchestrator not running${NC}"
fi

echo -e "\n${YELLOW}Stopping Slack bot...${NC}"
if pgrep -f "slack_bot.py" > /dev/null; then
    pkill -f "slack_bot.py"
    echo -e "  ${GREEN}Slack bot stopped${NC}"
else
    echo -e "  ${RED}Slack bot not running${NC}"
fi

sleep 2

# Verify
echo -e "\n${GREEN}Verification:${NC}"
if pgrep -f "hightrade_orchestrator.py\|slack_bot.py" > /dev/null; then
    echo -e "  ${RED}WARNING: Some processes still running${NC}"
    ps aux | grep -E "(slack_bot|orchestrator)" | grep -v grep
else
    echo -e "  ${GREEN}All processes stopped successfully${NC}"
fi

echo -e "\n${YELLOW}========================================${NC}"
