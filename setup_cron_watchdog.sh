#!/bin/bash
# Setup cron job to keep HighTrade processes alive

WATCHDOG_SCRIPT="/Users/stantonhigh/Documents/hightrade/watchdog.sh"
CRON_LOG="/Users/stantonhigh/Documents/hightrade/trading_data/logs/watchdog.log"

echo "Setting up cron watchdog for HighTrade..."

# Check if cron entry already exists
if crontab -l 2>/dev/null | grep -q "watchdog.sh"; then
    echo "⚠️  Watchdog cron job already exists"
    echo "Current crontab:"
    crontab -l | grep watchdog
    exit 0
fi

# Add to crontab - run every 5 minutes
(crontab -l 2>/dev/null; echo "*/5 * * * * cd /Users/stantonhigh/Documents/hightrade && ./watchdog_check.sh >> $CRON_LOG 2>&1") | crontab -

echo "✅ Watchdog cron job added!"
echo "   Runs every 5 minutes"
echo "   Log: $CRON_LOG"
echo ""
echo "To view crontab: crontab -l"
echo "To remove: crontab -l | grep -v watchdog | crontab -"
