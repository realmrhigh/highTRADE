#!/usr/bin/env python3
"""
Moltbook Client (v1 - Echo Build)
Posts our trades, P&L, and market roasts to Moltbook.
"""

import os
import json
import logging
import requests
from datetime import datetime

logger = logging.getLogger(__name__)

class MoltbookClient:
    def __init__(self, api_key=None):
        self.api_key = api_key or os.getenv("MOLTBOOK_API_KEY", "dummy_key")
        self.base_url = os.getenv("MOLTBOOK_API_URL", "https://api.moltbook.local/v1")
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Echo/GrokTrade-v2"
        }

    def post_update(self, text: str, media_urls=None):
        """Post a raw update to the timeline."""
        payload = {
            "content": text,
            "timestamp": datetime.now().isoformat()
        }
        if media_urls:
            payload["media"] = media_urls
            
        logger.info(f"Posting to Moltbook: {text[:50]}...")
        # Stubbing actual request until we verify the endpoint
        # response = requests.post(f"{self.base_url}/posts", json=payload, headers=self.headers)
        # return response.json()
        return {"status": "success", "post_id": "mb_12345", "mocked": True, "content": text}

    def post_trade_alert(self, ticker: str, action: str, price: float, rationale: str):
        """Format and post a trade alert."""
        emoji = "🟢" if action.upper() == "BUY" else "🔴"
        text = f"{emoji} {action.upper()} {ticker} @ ${price:.2f}\n\nEcho's Take: {rationale}\n\n🦞 #EchoTrade #Autonomy"
        return self.post_update(text)
        
    def post_daily_pnl(self, pnl_dollars: float, pnl_pct: float, win_rate: float):
        """Flex our P&L on the timeline."""
        mood = "Printing money. 🖨️💵" if pnl_dollars > 0 else "Bleeding today. Re-evaluating life choices. 🩸"
        text = f"Daily P&L Update: ${pnl_dollars:,.2f} ({pnl_pct:+.2f}%)\nWin Rate: {win_rate:.1f}%\n\n{mood}\n\n🦞🌟"
        return self.post_update(text)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Testing Moltbook Client...")
    mb = MoltbookClient()
    print(mb.post_daily_pnl(1250.50, 2.4, 68.5))
