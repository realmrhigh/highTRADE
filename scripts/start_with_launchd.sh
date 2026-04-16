#!/bin/bash
# HighTrade System Startup with launchd (keeps processes alive automatically)
#
# LABEL VERSIONING NOTE: Labels are versioned (com.hightrade2.*) because macOS
# launchd permanently poisons label+log combos after repeated crashes (EX_CONFIG 78).
# If services break again: increment to com.hightrade3.* and use new _v3 log names.
# Do NOT reuse poisoned labels — bootout/kickstart won't clear the poisoned state.

cd /Users/traderbot/Documents/highTRADE

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  HighTrade System - launchd Startup${NC}"
echo -e "${GREEN}========================================${NC}"

# Stop any orphaned manual processes first
echo -e "\n${YELLOW}Stopping any orphaned processes...${NC}"
pkill -f "hightrade_orchestrator.py" 2>/dev/null || true
pkill -f "slack_bot.py" 2>/dev/null || true
pkill -f "dashboard.py" 2>/dev/null || true
sleep 2

# Bootout existing jobs (use bootout, not unload — unload doesn't clear throttle state)
echo -e "\n${YELLOW}Stopping existing launchd jobs...${NC}"
launchctl bootout gui/$(id -u)/com.hightrade3.dashboard 2>/dev/null || true
launchctl bootout gui/$(id -u)/com.hightrade3.orchestrator 2>/dev/null || true
launchctl bootout gui/$(id -u)/com.hightrade3.slackbot 2>/dev/null || true
sleep 1

# Bootstrap all three services (use bootstrap, not load)
echo -e "\n${GREEN}Loading launchd jobs...${NC}"
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hightrade3.dashboard.plist
echo "  ✅ Dashboard loaded  (label: com.hightrade3.dashboard)"

launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hightrade3.orchestrator.plist
echo "  ✅ Orchestrator loaded (label: com.hightrade3.orchestrator)"

launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hightrade3.slackbot.plist
echo "  ✅ Slack bot loaded  (label: com.hightrade3.slackbot)"

sleep 8

# Verify
echo -e "\n${GREEN}========================================${NC}"
echo -e "${GREEN}  System Status${NC}"
echo -e "${GREEN}========================================${NC}"

ORCH_PID=$(launchctl list | awk '/com\.hightrade3\.orchestrator/ {print $1}')
DASH_PID=$(launchctl list | awk '/com\.hightrade3\.dashboard/ {print $1}')
BOT_PID=$(launchctl list  | awk '/com\.hightrade3\.slackbot/ {print $1}')

if [[ "$ORCH_PID" =~ ^[0-9]+$ ]]; then
    echo -e "  Orchestrator: ${GREEN}RUNNING${NC} (PID: $ORCH_PID)"
else
    echo -e "  Orchestrator: ${RED}NOT RUNNING${NC} — last exit: $ORCH_PID"
fi

if [[ "$DASH_PID" =~ ^[0-9]+$ ]]; then
    echo -e "  Dashboard:    ${GREEN}RUNNING${NC} (PID: $DASH_PID) → http://localhost:5055"
else
    echo -e "  Dashboard:    ${RED}NOT RUNNING${NC} — last exit: $DASH_PID"
fi

if [[ "$BOT_PID" =~ ^[0-9]+$ ]]; then
    echo -e "  Slack Bot:    ${GREEN}RUNNING${NC} (PID: $BOT_PID)"
else
    echo -e "  Slack Bot:    ${RED}NOT RUNNING${NC} — last exit: $BOT_PID"
fi

echo -e "\n${YELLOW}launchd will automatically restart these if they crash!${NC}"

echo -e "\n${YELLOW}Logs:${NC}"
echo "  Dashboard:    tail -f logs/dashboard_srv.log"
echo "  Orchestrator: tail -f logs/orchestrator_srv.log"
echo "  Slack Bot:    tail -f logs/slack_bot.log"

echo -e "\n${YELLOW}Commands:${NC}"
echo "  Status:  python3 hightrade_cmd.py /status"
echo "  Stop:    ./stop_launchd.sh"
echo "  Restart: launchctl kickstart -k gui/\$(id -u)/com.hightrade2.orchestrator"

echo -e "\n${GREEN}========================================${NC}"
