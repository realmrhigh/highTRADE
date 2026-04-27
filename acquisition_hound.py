from trading_db import get_sqlite_conn
#!/usr/bin/env python3
"""
acquisition_hound.py — Grok Hound module for HighTrade.
Scans for high-alpha, short-squeeze, and asymmetric upside opportunities using X data.
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set

import requests

from grok_client import GrokClient
from uw_fda_calendar import FDACalendar

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Unusual Whales API helpers
# ---------------------------------------------------------------------------

_UW_BASE = "https://api.unusualwhales.com"
_UW_API_KEY: Optional[str] = None
_UW_KEY_LOGGED = False  # log missing key only once


def _load_uw_key() -> Optional[str]:
    global _UW_API_KEY, _UW_KEY_LOGGED
    if _UW_API_KEY is not None:
        return _UW_API_KEY
    creds_path = Path.home() / ".openclaw" / "creds" / "unusualwhales.env"
    try:
        for line in creds_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("UW_API_KEY"):
                _UW_API_KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
                return _UW_API_KEY
    except Exception:
        pass
    if not _UW_KEY_LOGGED:
        logger.warning("UW: unusualwhales.env not found or missing UW_API_KEY — UW enrichment disabled")
        _UW_KEY_LOGGED = True
    return None


def _uw_get(path: str, params: Optional[Dict] = None) -> Optional[Dict]:
    """Authenticated GET to Unusual Whales API. Returns parsed JSON or None."""
    key = _load_uw_key()
    if not key:
        return None
    url = f"{_UW_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {key}",
        "UW-CLIENT-API-ID": "100001",
    }
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.debug(f"UW request failed [{path}]: {e}")
        return None


# ---------------------------------------------------------------------------

class GrokHound:
    """Elite high-alpha opportunity hunter focusing on asymmetric upside and X velocity."""

    def __init__(self, db_path: str = "trading_data/trading_history.db"):
        self.client = GrokClient()
        self.db_path = db_path
        self._ensure_table()

    def _ensure_table(self):
        """Add table creation safety."""
        conn = get_sqlite_conn(str(self.db_path), timeout=15)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS grok_hound_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL UNIQUE,
                    alpha_score INTEGER,
                    why_next TEXT,
                    signals TEXT,
                    risks TEXT,
                    action_suggestion TEXT,
                    breakout_window TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Safe migration: add breakout_window if it doesn't exist yet
            try:
                conn.execute("ALTER TABLE grok_hound_candidates ADD COLUMN breakout_window TEXT")
            except Exception:
                pass  # Column already exists
            conn.commit()
        finally:
            conn.close()

    def _get_exclude_list(self) -> Set[str]:
        """Fetch tickers excluded from Hound recommendations.

        Exclusion rules:
        - Open positions: always excluded.
        - Active conditional tracking (broker is live-watching): always excluded.
        - Manually ignored in Hound table: always excluded (intentional, no TTL).
        - Acquisition watchlist items: 48h TTL only — previously these were excluded
          indefinitely (session-long), which caused the list to balloon to 145+ tickers.
        - Analyst-pass items: 12h TTL.
        """
        exclude = set()
        forty_eight_hours_ago = (datetime.now() - timedelta(hours=48)).strftime('%Y-%m-%d %H:%M:%S')
        twelve_hours_ago = (datetime.now() - timedelta(hours=12)).strftime('%Y-%m-%d %H:%M:%S')
        try:
            conn = get_sqlite_conn(str(self.db_path), timeout=15)
            try:
                cursor = conn.cursor()

                # 1. Watchlist items within 48h TTL (not ALL non-archived items)
                cursor.execute("""
                    SELECT ticker FROM acquisition_watchlist
                    WHERE status != 'archived' AND created_at > ?
                """, (forty_eight_hours_ago,))
                exclude.update([row[0].upper() for row in cursor.fetchall() if row[0]])

                # 2. Active conditional tracking (broker is watching these)
                cursor.execute("SELECT ticker FROM conditional_tracking WHERE status = 'active'")
                exclude.update([row[0].upper() for row in cursor.fetchall() if row[0]])

                # 3. Manually ignored in Hound table (permanent until un-ignored)
                cursor.execute("SELECT ticker FROM grok_hound_candidates WHERE status = 'ignored'")
                exclude.update([row[0].upper() for row in cursor.fetchall() if row[0]])

                # 4. Open positions (always skip — we already own these)
                cursor.execute("SELECT asset_symbol FROM trade_records WHERE status = 'open'")
                exclude.update([row[0].upper() for row in cursor.fetchall() if row[0]])

                # 5. Analyst-pass within 12h TTL
                cursor.execute("""
                    SELECT ticker FROM acquisition_watchlist
                    WHERE status = 'analyst_pass' AND created_at > ?
                """, (twelve_hours_ago,))
                exclude.update([row[0].upper() for row in cursor.fetchall() if row[0]])

            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"Could not fetch exclude list: {e}")

        return exclude

    def _catalyst_bypass_set(self, candidates: Set[str]) -> Set[str]:
        """Return the subset of candidates that should bypass exclusion due to a new catalyst.

        Uses UW stock-state endpoint to detect >5% moves since previous close.
        Falls back silently on any error. Caps at 30 tickers to avoid scan latency.
        """
        bypass: Set[str] = set()
        if not candidates:
            return bypass
        check = sorted(candidates)[:30]
        for ticker in check:
            try:
                data = _uw_get(f"/api/stock/{ticker}/stock-state")
                if data is None:
                    continue
                # UW response may nest under 'data' key
                if isinstance(data, dict) and 'data' in data:
                    data = data['data']
                curr = data.get('last_price') or data.get('regular_market_price')
                prev = data.get('prev_day_close_price') or data.get('previous_close')
                if curr is not None and prev is not None and float(prev) > 0:
                    move_pct = abs((float(curr) - float(prev)) / float(prev)) * 100
                    if move_pct > 5.0:
                        logger.info(
                            f"  🚨 Catalyst bypass: {ticker} moved {move_pct:.1f}% since last close"
                            " — re-admitting for Hound scan"
                        )
                        bypass.add(ticker)
            except Exception:
                pass  # individual ticker failures are non-fatal
        return bypass

    def _fetch_uw_flow_context(self) -> Dict:
        """Fetch recent unusual options flow and dark pool prints from UW.

        Returns:
            flow_summary: list of {ticker, side, premium, sentiment}
            darkpool_summary: list of {ticker, size, price}
            hot_tickers: set of tickers appearing in either feed
        """
        result = {
            "flow_summary": [],
            "darkpool_summary": [],
            "hot_tickers": set(),
        }
        try:
            flow_data = _uw_get("/api/option-trades/flow-alerts", params={"limit": 50})
            if flow_data:
                items = flow_data if isinstance(flow_data, list) else flow_data.get('data', [])
                for item in items[:50]:
                    ticker = (item.get('ticker') or item.get('symbol') or '').upper()
                    if not ticker:
                        continue
                    entry = {
                        "ticker": ticker,
                        "side": item.get('put_call') or item.get('side') or item.get('type', ''),
                        "premium": item.get('premium') or item.get('total_premium') or item.get('cost', 0),
                        "sentiment": item.get('sentiment') or item.get('bullish_bearish') or '',
                    }
                    result["flow_summary"].append(entry)
                    result["hot_tickers"].add(ticker)
        except Exception as e:
            logger.warning(f"UW flow-alerts fetch failed: {e}")

        try:
            dp_data = _uw_get("/api/darkpool/recent", params={"limit": 20})
            if dp_data:
                items = dp_data if isinstance(dp_data, list) else dp_data.get('data', [])
                for item in items[:20]:
                    ticker = (item.get('ticker') or item.get('symbol') or '').upper()
                    if not ticker:
                        continue
                    entry = {
                        "ticker": ticker,
                        "size": item.get('size') or item.get('volume') or item.get('quantity', 0),
                        "price": item.get('price') or item.get('executed_price', 0),
                    }
                    result["darkpool_summary"].append(entry)
                    result["hot_tickers"].add(ticker)
        except Exception as e:
            logger.warning(f"UW darkpool fetch failed: {e}")

        logger.info(
            f"  📡 UW context: {len(result['flow_summary'])} flow alerts, "
            f"{len(result['darkpool_summary'])} dark pool prints, "
            f"{len(result['hot_tickers'])} hot tickers"
        )
        return result

    def _get_post_catalyst_tickers(self) -> List[str]:
        """
        Return low-float names that had a +30-50% move in the past 7-14 days and are now
        consolidating — prime candidates for a second leg if X velocity picks up.
        Pulls from conditional_tracking (active setups) and recent acquisition_watchlist entries.
        """
        tickers = []
        try:
            conn = get_sqlite_conn(str(self.db_path), timeout=15)
            try:
                cursor = conn.cursor()
                cutoff = (datetime.now() - timedelta(days=14)).strftime('%Y-%m-%d %H:%M:%S')
                # Active conditional setups added in the last 14 days that came from a big-move catalyst
                cursor.execute("""
                    SELECT ticker FROM conditional_tracking
                    WHERE status = 'active'
                      AND created_at > ?
                      AND (watch_tag LIKE '%momentum%' OR watch_tag LIKE '%breakout%'
                           OR watch_tag LIKE '%squeeze%' OR watch_tag LIKE '%catalyst%')
                """, (cutoff,))
                tickers.extend([row[0].upper() for row in cursor.fetchall() if row[0]])
                # Recent grok_hound_candidates that hit ≥70 alpha and are now watched/expired
                cursor.execute("""
                    SELECT ticker FROM grok_hound_candidates
                    WHERE alpha_score >= 70
                      AND created_at > ?
                      AND status IN ('watched', 'expired')
                """, (cutoff,))
                tickers.extend([row[0].upper() for row in cursor.fetchall() if row[0]])
                # Reverse-split names from acquisition_watchlist (last 14 days)
                cursor.execute("""
                    SELECT ticker FROM acquisition_watchlist
                    WHERE created_at > ?
                      AND (notes LIKE '%reverse split%' OR notes LIKE '%1-for-%'
                           OR entry_conditions LIKE '%reverse split%')
                      AND status != 'archived'
                """, (cutoff,))
                tickers.extend([row[0].upper() for row in cursor.fetchall() if row[0]])
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"Could not build post-catalyst list: {e}")
        return list(dict.fromkeys(tickers))  # dedupe, preserve order

    def _uw_validate_candidates(self, candidates: List[Dict]) -> List[Dict]:
        """Adjust alpha_score for each candidate based on UW per-ticker flow-recent.

        Net bullish call flow → +5 (cap 100).
        Net bearish put flow  → -5 (floor 0).
        Logs each adjustment. Never blocks — silently skips on any error.
        """
        if not _load_uw_key():
            return candidates
        for c in candidates:
            ticker = (c.get('ticker') or '').upper().strip()
            if not ticker:
                continue
            try:
                data = _uw_get(f"/api/stock/{ticker}/flow-recent")
                if data is None:
                    continue
                items = data if isinstance(data, list) else data.get('data', [])
                call_premium = 0.0
                put_premium = 0.0
                for item in (items or []):
                    side = (item.get('put_call') or item.get('side') or item.get('type') or '').lower()
                    prem = float(item.get('premium') or item.get('total_premium') or item.get('cost') or 0)
                    if 'call' in side:
                        call_premium += prem
                    elif 'put' in side:
                        put_premium += prem
                if call_premium > put_premium:
                    old = c.get('alpha_score', 0)
                    c['alpha_score'] = min(100, old + 5)
                    logger.info(
                        f"  📈 UW flow boost: {ticker} net bullish calls "
                        f"(${call_premium:,.0f} vs ${put_premium:,.0f}) → alpha {old} → {c['alpha_score']}"
                    )
                elif put_premium > call_premium:
                    old = c.get('alpha_score', 0)
                    c['alpha_score'] = max(0, old - 5)
                    logger.info(
                        f"  📉 UW flow cut: {ticker} net bearish puts "
                        f"(${put_premium:,.0f} vs ${call_premium:,.0f}) → alpha {old} → {c['alpha_score']}"
                    )
            except Exception as e:
                logger.debug(f"UW flow-recent check failed for {ticker}: {e}")
        return candidates

    def hunt(self, current_state: Dict, focus_tickers: List[str] = None) -> Dict:
        """
        Queries Grok for the next high-alpha setups, excluding known tickers.
        """
        exclude_list = self._get_exclude_list()
        # Bypass tickers with significant new price catalysts (>5% move since last close)
        bypass = self._catalyst_bypass_set(exclude_list)
        if bypass:
            logger.info(f"  🔓 Catalyst bypass removing {len(bypass)} ticker(s) from exclusion: {bypass}")
            exclude_list -= bypass
        post_catalyst = self._get_post_catalyst_tickers()
        logger.info(f"🐕 Grok Hound is on the scent... excluding {len(exclude_list)} tickers, {len(post_catalyst)} post-catalyst candidates.")

        # ── Unusual Whales enrichment ──────────────────────────────────────
        uw_context: Dict = {"flow_summary": [], "darkpool_summary": [], "hot_tickers": set()}
        try:
            uw_context = self._fetch_uw_flow_context()
        except Exception as e:
            logger.warning(f"UW context fetch failed: {e}")

        hot_tickers_list = sorted(uw_context.get("hot_tickers", set()))

        # Build concise text summaries for the Grok payload (top 20)
        flow_text_items = []
        for item in uw_context.get("flow_summary", [])[:20]:
            flow_text_items.append(
                f"{item.get('ticker')} {item.get('side','?')} ${item.get('premium',0):,.0f} {item.get('sentiment','')}"
            )
        uw_flow_text = "; ".join(flow_text_items) if flow_text_items else "none"

        dp_text_items = []
        for item in uw_context.get("darkpool_summary", []):
            dp_text_items.append(
                f"{item.get('ticker')} {item.get('size',0):,} shares @ ${item.get('price',0)}"
            )
        uw_dp_text = "; ".join(dp_text_items) if dp_text_items else "none"
        # ──────────────────────────────────────────────────────────────────

        system_prompt = f"""
        You are Grok Hound — short-term momentum hunter for HighTrade.
        The system strategy is FLIP AND BANK: buy dips/breakouts, ride for 1-5 days, take profit, redeploy.
        We are NOT a long-term value fund. We do NOT hold recovery plays. We flip.
        Lead model is Gemini 3.1. Output STRICT JSON only.

        UW options flow and dark pool data is provided in the payload. Heavily weight any candidate
        that appears in uw_hot_tickers or uw_flow_alerts — that is real smart money activity.

        STRATEGY:
        Find setups that can move in the NEXT 1-5 TRADING DAYS with a clear, specific catalyst.
        Score 0-100 on "alpha_score" based purely on SHORT-TERM probability of a 3-8% move.

        ✅ WHAT WE WANT:
        - Momentum plays: stocks that are ALREADY moving with volume — ride the wave
        - Short squeeze setups: high SI + unusual call buying + price pressure
        - Earnings reactions: buying the dip/breakout in the 48h window around earnings
        - Catalyst events: product launches, FDA decisions, contract wins — within 48h
        - Sector rotations: money visibly flowing INTO a sector today
        - Crisis commodities: energy/commodity plays with active macro tailwind (e.g. USO, XLE during oil spike)
        - Retail velocity plays: meme revival, unusual volume, social spike with price action confirming
        - Post-catalyst consolidation plays: low-float names (<5M shares) that already moved +30-50%
          in the past 7-14 days and are now coiling. Apply +10 score boost for any modest X velocity
          during the pullback (reversal watch chatter, squeeze talk, unusual volume commentary).
          Post-catalyst tickers to rescan: {', '.join(post_catalyst) if post_catalyst else 'None identified yet'}
        - Dark catalyst setups: low-float names with recent clinical/regulatory news + any financing
          event (private placement, S-1 withdrawal, registered direct, at-the-market offering,
          debt retirement, debt settlement via equity, balance sheet cleanup).
          News is THE signal when float is this tight. Score ≥65 if float <2M and a capital-raise
          or debt-removal PR appeared within 48h of a prior catalyst.
        - Reverse-split low-float runners: post-reverse-split names with float <3M. Even with zero
          X pre-fuel, score ≥65 if the split was effective within the past 14 days and any modest
          X velocity, pipeline mention, or unusual volume appears. Apply +15 score boost.
        - Pipeline/license dark catalysts: low-float names (<2M float) with a license agreement,
          exclusive worldwide rights, strategic collaboration, or pipeline expansion PR. Score ≥65
          even with no X chatter — news is the whole signal here.
        - Financing + debt overhang removal: flag ≥65 as dark-catalyst when float <2M and news
          shows debt retirement or insider debenture conversion — removes the dilution ceiling and
          can unlock violent upside with no prior retail buzz.

        ❌ DO NOT RECOMMEND:
        - NVDA, AAPL, MSFT, GOOGL, META, AMZN, TSLA as recovery/mean-reversion plays
          (only nominate these if there is a SPECIFIC catalyst firing within 48 hours)
        - Any stock where the thesis is "it's cheap vs its 52w high" — that's a recovery bet, not a trade
        - Any setup requiring >5 trading days to play out
        - Stocks you cannot name a specific catalyst date/event for

        Prioritize US stocks. Ignore pure crypto.

        FDA calendar events this week are provided — biotech plays near PDUFA dates get +10 alpha score boost.

        EXCLUDE THE FOLLOWING TICKERS (already being handled):
        {', '.join(list(exclude_list)) if exclude_list else 'None'}

        Respond with ONLY valid JSON in this structure:
        {{
          "candidates": [
            {{
              "ticker": "SYMBOL",
              "alpha_score": int 0-100,
              "why_next": "specific catalyst + expected move in next 1-5 days",
              "signals": ["X chatter spike", "high short interest", "unusual call buying", etc],
              "risks": ["dilution", "pump and dump", etc],
              "action_suggestion": "add_to_watch|monitor|buy_small",
              "breakout_window": "human-readable estimated timeframe for the setup to fire, e.g. '2026-04-28 premarket', '2026-04-29 intraday', 'this week', 'today'. Base on: upcoming earnings/FDA dates, options expiry clustering, how coiled the setup looks, and flow momentum."
            }}
          ],
          "hound_mood": "aggressive|cautious|neutral",
          "market_chatter_summary": "1-sentence summary of retail sentiment"
        }}
        """

        # ── FDA calendar enrichment ───────────────────────────────────────────
        fda_events_text = "none"
        try:
            _fda = FDACalendar()
            fda_events_this_week = _fda.get_events_this_week()
            fda_events_text = _fda.format_for_prompt(fda_events_this_week)
            if fda_events_this_week:
                logger.info(f"  💊 FDA calendar: {len(fda_events_this_week)} event(s) this week")
        except Exception as _fe:
            logger.debug(f"FDA calendar fetch failed: {_fe}")
        # ─────────────────────────────────────────────────────────────────────

        payload = {
            "current_defcon": current_state.get("defcon_level"),
            "macro_score": current_state.get("macro_score"),
            "watchlist": current_state.get("watchlist", []),
            "focus_tickers": focus_tickers,
            "recent_signals": current_state.get("latest_gemini_briefing_summary", ""),
            "low_float_financing_alert": current_state.get("low_float_financing_alert", False),
            "reverse_split_alert": current_state.get("reverse_split_alert", False),
            "pipeline_deal_alert": current_state.get("pipeline_deal_alert", False),
            "post_catalyst_tickers": post_catalyst,
            "uw_flow_alerts": uw_flow_text,
            "uw_hot_tickers": hot_tickers_list,
            "uw_darkpool": uw_dp_text,
            "fda_events_this_week": fda_events_text,
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
        except json.JSONDecodeError:
            logger.error(f"  ❌ Hound failed to parse findings: {text[:200]}")
            return {"candidates": []}

        # ── UW per-candidate validation ────────────────────────────────────
        try:
            results['candidates'] = self._uw_validate_candidates(results.get('candidates', []))
        except Exception as e:
            logger.warning(f"UW candidate validation failed: {e}")
        # ──────────────────────────────────────────────────────────────────

        logger.info(f"  ✅ Hound found {len(results.get('candidates', []))} potential candidates.")
        return results

    def save_candidates(self, results: Dict):
        """Persist findings to DB with UPSERT logic and auto-promote alpha >= 85."""
        candidates = results.get('candidates', [])
        if not candidates:
            return

        conn = get_sqlite_conn(str(self.db_path), timeout=15)
        try:
            cursor = conn.cursor()

            # ── Expire stale hound candidates (48h TTL) ───────────────────────
            # Entries older than 48h are reset to 'expired' so the next Hound run
            # starts fresh — prevents the list from accumulating indefinitely.
            cutoff_48h = (datetime.now() - timedelta(hours=48)).strftime('%Y-%m-%d %H:%M:%S')
            expired = cursor.execute("""
                UPDATE grok_hound_candidates
                SET status = 'expired'
                WHERE created_at < ?
                  AND status NOT IN ('ignored', 'expired')
            """, (cutoff_48h,)).rowcount
            if expired:
                logger.info(f"  🕰️  Hound: expired {expired} stale candidates (>48h)")
            conn.commit()

            date_str = datetime.now().strftime('%Y-%m-%d')
            promoted_tickers = []

            for c in candidates:
                ticker = c.get('ticker', '').upper().strip()
                if not ticker: continue

                score = c.get('alpha_score', 0)

                # Use UPSERT (INSERT ... ON CONFLICT)
                cursor.execute("""
                    INSERT INTO grok_hound_candidates
                    (ticker, alpha_score, why_next, signals, risks, action_suggestion, breakout_window, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
                    ON CONFLICT(ticker) DO UPDATE SET
                        alpha_score = excluded.alpha_score,
                        why_next = excluded.why_next,
                        signals = excluded.signals,
                        risks = excluded.risks,
                        action_suggestion = excluded.action_suggestion,
                        breakout_window = excluded.breakout_window,
                        created_at = CURRENT_TIMESTAMP
                    WHERE status != 'ignored' AND status != 'watched'
                """, (
                    ticker,
                    score,
                    c.get('why_next', ''),
                    json.dumps(c.get('signals', [])),
                    json.dumps(c.get('risks', [])),
                    c.get('action_suggestion', 'monitor'),
                    c.get('breakout_window', '')
                ))

                # --- AUTO-PROMOTION LOGIC ---
                # Tiered thresholds by action:
                #   buy_small    → 60 (short-term speculative, fast lane)
                #   add_to_watch → 65 (moderate conviction, full pipeline)
                #   monitor/other → 72 (higher bar before spending analyst quota)
                _action_lower = (c.get('action_suggestion') or '').lower()
                if _action_lower == 'buy_small':
                    _promo_threshold = 60
                elif _action_lower == 'add_to_watch':
                    _promo_threshold = 65
                else:
                    _promo_threshold = 72
                if score >= _promo_threshold:
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
            logger.info(f"  💾 Processed {len(candidates)} candidates. Auto-promoted: {promoted_tickers}")
            return promoted_tickers
        finally:
            conn.close()

def run_hound_cycle(extra_context: Optional[Dict] = None) -> Dict:
    """Module-level entry point for triggering a Grok Hound scan.

    Used by ollama_client._exec_run_hound and the force-trigger script.
    """
    hound = GrokHound()
    state = {
        "defcon_level": (extra_context or {}).get("defcon_level", 3),
        "macro_score":  (extra_context or {}).get("news_score", 50),
        "watchlist": [],
        "low_float_financing_alert": False,
        "reverse_split_alert": False,
        "pipeline_deal_alert": False,
    }
    results = hound.hunt(state)
    hound.save_candidates(results)
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    hound = GrokHound()
    # Mock state
    res = hound.hunt({"defcon_level": 4, "macro_score": 55})
    print(json.dumps(res, indent=2))
    hound.save_candidates(res)
