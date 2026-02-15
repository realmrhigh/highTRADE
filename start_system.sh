#!/bin/bash
# HighTrade System Startup Script
# Starts both the orchestrator and Slack bot in the background with proper logging

set -e

cd /Users/stantonhigh/Documents/hightrade

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  HighTrade System Startup${NC}"
echo -e "${GREEN}========================================${NC}"

# Kill any existing processes
echo -e "\n${YELLOW}Checking for existing processes...${NC}"
if pgrep -f "hightrade_orchestrator.py" > /dev/null; then
    echo "Stopping existing orchestrator..."
    pkill -f "hightrade_orchestrator.py"
    sleep 2
fi

if pgrep -f "slack_bot.py" > /dev/null; then
    echo "Stopping existing Slack bot..."
    pkill -f "slack_bot.py"
    sleep 2
fi

# Create logs directory
mkdir -p trading_data/logs

# Start orchestrator
echo -e "\n${GREEN}Starting HighTrade Orchestrator...${NC}"
nohup python3 hightrade_orchestrator.py continuous 15 \
    > trading_data/logs/orchestrator_output.log \
    2> trading_data/logs/orchestrator_error.log &
ORCH_PID=$!
echo "  PID: $ORCH_PID"

# Give orchestrator time to start
sleep 3

# Start Slack bot
echo -e "\n${GREEN}Starting Slack Bot...${NC}"
nohup python3 slack_bot.py \
    > trading_data/logs/slack_bot_output.log \
    2> trading_data/logs/slack_bot_error.log &
BOT_PID=$!
echo "  PID: $BOT_PID"

# Wait and verify
sleep 3

echo -e "\n${GREEN}========================================${NC}"
echo -e "${GREEN}  System Status${NC}"
echo -e "${GREEN}========================================${NC}"

if ps -p $ORCH_PID > /dev/null; then
    echo -e "  Orchestrator: ${GREEN}RUNNING${NC} (PID: $ORCH_PID)"
else
    echo -e "  Orchestrator: ${RED}FAILED${NC}"
    echo "  Check logs: tail -f trading_data/logs/orchestrator_error.log"
fi

if ps -p $BOT_PID > /dev/null; then
    echo -e "  Slack Bot:    ${GREEN}RUNNING${NC} (PID: $BOT_PID)"
else
    echo -e "  Slack Bot:    ${RED}FAILED${NC}"
    echo "  Check logs: tail -f trading_data/logs/slack_bot_error.log"
fi

echo -e "\n${YELLOW}Logs:${NC}"
echo "  Orchestrator: tail -f trading_data/logs/orchestrator_error.log"
echo "  Slack Bot:    tail -f trading_data/logs/slack_bot_error.log"
echo "  Main Log:     tail -f trading_data/logs/hightrade_$(date +%Y%m%d).log"

echo -e "\n${YELLOW}Commands:${NC}"
echo "  Status:  python3 hightrade_cmd.py /status"
echo "  Stop:    ./stop_system.sh"

echo -e "\n${GREEN}========================================${NC}"
