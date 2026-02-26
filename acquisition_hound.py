#!/usr/bin/env python3
"""
acquisition_hound.py — Grok Hound module for HighTrade.
Scans for high-alpha, short-squeeze, and asymmetric upside opportunities using X data.
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set

from grok_client import GrokClient

logger = logging.getLogger(__name__)

class GrokHound:
    """Elite high-alpha opportunity hunter focusing on asymmetric upside and X velocity."""

    def __init__(self, db_path: str = "trading_data/trading_history.db"):
        self.client = GrokClient()
        self.db_path = db_path
        self._ensure_table()

    def _ensure_table(self):
        """Add table creation safety."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS grok_hound_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL UNIQUE,
                alpha_score INTEGER,
                why_next TEXT,
                signals TEXT,
                risks TEXT,
                action_suggestion TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()

    def _get_exclude_list(self) -> Set[str]:
        """Fetch tickers that are already being watched, ignored, or analyzed."""
        exclude = set()
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 1. Tickers already in the acquisition watchlist (excluding those that are pending research)
            # We skip 'pending' so they don't get re-added, but they are technically 'being handled'
            cursor.execute("SELECT ticker FROM acquisition_watchlist WHERE status != 'archived'")
            exclude.update([row[0].upper() for row in cursor.fetchall() if row[0]])
            
            # 2. Tickers in active conditional tracking (being watched by broker)
            cursor.execute("SELECT ticker FROM conditional_tracking WHERE status = 'active'")
            exclude.update([row[0].upper() for row in cursor.fetchall() if row[0]])
            
            # 3. Tickers manually ignored in the Hound table
            cursor.execute("SELECT ticker FROM grok_hound_candidates WHERE status = 'ignored'")
            exclude.update([row[0].upper() for row in cursor.fetchall() if row[0]])

            # 4. CURRENT OPEN POSITIONS (Tracked independently, skip from acquisition)
            cursor.execute("SELECT asset_symbol FROM trade_records WHERE status = 'open'")
            exclude.update([row[0].upper() for row in cursor.fetchall() if row[0]])

            # 5. 12-Hour HOLD for analyst_pass items
            # Tickers that the analyst passed on within the last 12 hours
            twelve_hours_ago = (datetime.now() - timedelta(hours=12)).strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute("""
                SELECT ticker FROM acquisition_watchlist 
                WHERE status = 'analyst_pass' AND created_at > ?
            """, (twelve_hours_ago,))
            exclude.update([row[0].upper() for row in cursor.fetchall() if row[0]])
            
            conn.close()
        except Exception as e:
            logger.warning(f"Could not fetch exclude list: {e}")
            
        return exclude

    def hunt(self, current_state: Dict, focus_tickers: List[str] = None) -> Dict:
        """
        Queries Grok for the next high-alpha setups, excluding known tickers.
        """
        exclude_list = self._get_exclude_list()
        logger.info(f"🐕 Grok Hound is on the scent... excluding {len(exclude_list)} tickers.")

        system_prompt = f"""
        You are Grok Hound — elite high-alpha opportunity hunter for HighTrade acquisition team. 
        Focus on asymmetric upside setups with strong catalysts, unusual flow, rotation plays, short interest edges, or retail velocity.
        Lead model is Gemini 3.1. Output STRICT JSON only.
        
        TASK:
        Scan your real-time X data for high-conviction alpha setups.
        Score 0-100 on "alpha_score".
        Prioritize US stocks, ignore pure crypto.
        
        EXCLUDE THE FOLLOWING TICKERS (already being handled):
        {', '.join(list(exclude_list)) if exclude_list else 'None'}
        
        Respond with ONLY valid JSON in this structure:
        {{
          "candidates": [
            {{
              "ticker": "SYMBOL",
              "alpha_score": int 0-100,
              "why_next": "brief thesis",
              "signals": ["X chatter spike", "high short interest", etc],
              "risks": ["dilution", "pump and dump", etc],
              "action_suggestion": "add_to_watch|monitor|buy_small"
            }}
          ],
          "hound_mood": "aggressive|cautious|neutral",
          "market_chatter_summary": "1-sentence summary of retail sentiment"
        }}
        """

        payload = {
            "current_defcon": current_state.get("defcon_level"),
            "macro_score": current_state.get("macro_score"),
            "watchlist": current_state.get("watchlist", []),
            "focus_tickers": focus_tickers,
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
        """Persist findings to DB with UPSERT logic and auto-promote alpha >= 85."""
        candidates = results.get('candidates', [])
        if not candidates:
            return

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        date_str = datetime.now().strftime('%Y-%m-%d')
        promoted_tickers = []

        for c in candidates:
            ticker = c.get('ticker', '').upper().strip()
            if not ticker: continue
            
            score = c.get('alpha_score', 0)
            
            # Use UPSERT (INSERT ... ON CONFLICT)
            cursor.execute("""
                INSERT INTO grok_hound_candidates 
                (ticker, alpha_score, why_next, signals, risks, action_suggestion, status)
                VALUES (?, ?, ?, ?, ?, ?, 'pending')
                ON CONFLICT(ticker) DO UPDATE SET
                    alpha_score = excluded.alpha_score,
                    why_next = excluded.why_next,
                    signals = excluded.signals,
                    risks = excluded.risks,
                    action_suggestion = excluded.action_suggestion,
                    created_at = CURRENT_TIMESTAMP
                WHERE status != 'ignored' AND status != 'watched'
            """, (
                ticker,
                score,
                c.get('why_next', ''),
                json.dumps(c.get('signals', [])),
                json.dumps(c.get('risks', [])),
                c.get('action_suggestion', 'monitor')
            ))

            # --- AUTO-PROMOTION LOGIC ---
            # If Alpha is high (>= 75), send straight to research pipeline
            if score >= 75:
                # Check if already in watchlist
                cursor.execute("SELECT 1 FROM acquisition_watchlist WHERE ticker = ? AND status != 'archived'", (ticker,))
                if not cursor.fetchone():
                    logger.info(f"  🚀 AUTO-PROMOTING {ticker} (Alpha: {score}) to Acquisition Watchlist")
                    action = (c.get('action_suggestion') or '').upper().replace('_', ' ')
                    cursor.execute("""
                        INSERT OR REPLACE INTO acquisition_watchlist
                        (date_added, ticker, source, model_confidence, biggest_risk,
                         biggest_opportunity, entry_conditions, notes, status)
                        VALUES (?, ?, 'grok_hound_auto', ?, ?, ?, ?, ?, 'pending')
                    """, (
                        date_str,
                        ticker,
                        float(score) / 100.0,
                        json.dumps(c.get('risks', [])),
                        c.get('why_next', ''),
                        c.get('why_next', ''),          # thesis as entry_conditions
                        f"[{action}] Auto-promoted (Alpha {score})",
                    ))
                    # Mark as watched in hound table
                    cursor.execute("UPDATE grok_hound_candidates SET status = 'watched' WHERE ticker = ?", (ticker,))
                    promoted_tickers.append(ticker)
        
        conn.commit()
        conn.close()
        logger.info(f"  💾 Processed {len(candidates)} candidates. Auto-promoted: {promoted_tickers}")
        return promoted_tickers

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    hound = GrokHound()
    # Mock state
    res = hound.hunt({"defcon_level": 4, "macro_score": 55})
    print(json.dumps(res, indent=2))
    hound.save_candidates(res)
