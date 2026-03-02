#!/bin/bash
export HOME=/Users/stantonhigh
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
cd /Users/stantonhigh/Documents/hightrade
exec /opt/homebrew/bin/python3.11 hightrade_orchestrator.py continuous 15
