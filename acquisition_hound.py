#!/usr/bin/env python3
"""
acquisition_hound.py — Grok Hound module for HighTrade.
Scans for high-alpha, short-squeeze, and meme-explosion opportunities using X data.
"""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from grok_client import GrokClient

logger = logging.getLogger(__name__)

class GrokHound:
    """Elite opportunity hunter leveraging real-time X velocity and squeeze metrics."""

    def __init__(self, db_path: str = "trading_data/trading_history.db"):
        self.client = GrokClient()
        self.db_path = db_path

    def hunt(self, current_state: Dict) -> Dict:
        """
        Queries Grok for the next high-alpha/meme setups.
        """
        logger.info("🐕 Grok Hound is on the scent... scanning for high-alpha setups...")

        system_prompt = """
        You are Grok Hound — elite high alpha, meme short-squeeze, opportunity hunter for HighTrade.
        Lead model is Gemini 3.1. Output STRICT JSON only.
        
        TASK:
        Scan your real-time X data for GME-style setups: high X velocity + short interest + low float/gamma + retail frenzy + catalyst.
        Score 0-100 on "meme_explosion_potential".
        Prioritize US stocks, ignore pure crypto.
        
        Respond with ONLY valid JSON in this structure:
        {
          "candidates": [
            {
              "ticker": "SYMBOL",
              "meme_score": int 0-100,
              "why_next_gme": "brief thesis",
              "signals": ["X chatter spike", "high short interest", etc],
              "risks": ["dilution", "pump and dump", etc],
              "action_suggestion": "add_to_watch|monitor|buy_small"
            }
          ],
          "hound_mood": "aggressive|cautious|neutral",
          "market_chatter_summary": "1-sentence summary of retail sentiment"
        }
        """

        payload = {
            "current_defcon": current_state.get("defcon_level"),
            "macro_score": current_state.get("macro_score"),
            "watchlist": current_state.get("watchlist", []),
            "recent_signals": current_state.get("latest_gemini_briefing_summary", ""),
            "timestamp": datetime.now().isoformat()
        }

        text, in_tok, out_tok = self.client.call(
            json.dumps(payload),
            system_prompt=system_prompt,
            temperature=0.3
        )

        if not text:
            logger.warning("  ⚠️ Grok Hound returned empty-handed.")
            return {"candidates": []}

        # Clean JSON
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        try:
            results = json.loads(text)
            logger.info(f"  ✅ Hound found {len(results.get('candidates', []))} potential candidates.")
            return results
        except json.JSONDecodeError:
            logger.error(f"  ❌ Hound failed to parse findings: {text[:200]}")
            return {"candidates": []}

    def save_candidates(self, results: Dict):
        """Persist findings to DB and prevent duplicates."""
        candidates = results.get('candidates', [])
        if not candidates:
            return

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        for c in candidates:
            ticker = c.get('ticker', '').upper().strip()
            if not ticker: continue
            
            cursor.execute("""
                INSERT INTO grok_hound_candidates 
                (ticker, meme_score, why_next_gme, signals, risks, action_suggestion)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                ticker,
                c.get('meme_score', 0),
                c.get('why_next_gme', ''),
                json.dumps(c.get('signals', [])),
                json.dumps(c.get('risks', [])),
                c.get('action_suggestion', 'monitor')
            ))
        
        conn.commit()
        conn.close()
        logger.info(f"  💾 Saved {len(candidates)} candidates to grok_hound_candidates table.")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    hound = GrokHound()
    # Mock state
    res = hound.hunt({"defcon_level": 4, "macro_score": 55})
    print(json.dumps(res, indent=2))
    hound.save_candidates(res)
