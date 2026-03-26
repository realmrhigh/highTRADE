#!/bin/bash
echo "Restarting HighTrade Orchestrator..."
launchctl kickstart -k gui/$(id -u)/com.hightrade2.orchestrator
echo "Done. Orchestrator restarted."
sleep 2
