#!/usr/bin/env bash
# HighTrade Dashboard Publisher
# Generates a fresh snapshot and pushes it to GitHub Pages
# Usage:  ./publish_dashboard.sh [commit message]
#
# GitHub Pages URL: https://realmrhigh.github.io/highTRADE/

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

MSG="${1:-Dashboard snapshot $(date '+%Y-%m-%d %H:%M')}"

echo "⚡ HighTrade Dashboard Publisher"
echo "   Generating fresh snapshot..."
python3 dashboard.py

echo "   Copying to docs/index.html..."
cp trading_data/dashboard.html docs/index.html

echo "   Committing..."
git add docs/index.html
git commit -m "$MSG"

echo "   Pushing to GitHub..."
git push origin main

echo ""
echo "   ✅ Live at: https://realmrhigh.github.io/highTRADE/"
echo ""
