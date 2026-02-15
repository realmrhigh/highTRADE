#!/bin/bash
export HOME=/Users/stantonhigh
export PATH=/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin
cd /Users/stantonhigh/Documents/hightrade
exec /Applications/Xcode.app/Contents/Developer/usr/bin/python3 hightrade_orchestrator.py continuous 15
