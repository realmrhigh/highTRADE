#!/usr/bin/env python3
"""
day_trader.py — Grok-powered intraday trader for HighTrade.

Picks one stock per morning via Grok (with real-time web + X search),
buys at open with confidence-scaled sizing, and exits strategically
by close. Grok owns 100% of decisions.

Schedule (Eastern Time):
  7:00 AM   — Pre-market scan (Grok Responses API w/ web_search + x_search)
  9:35 AM   — Auto-buy (confidence-scaled from available cash)
  Every 15m — Check stop-loss / take-profit / stretch target
  3:50 PM   — Hard EOD exit backstop
"""

import json
import logging
import math
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from grok_client import GrokClient

logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent.resolve()
DB_PATH = SCRIPT_DIR / 'trading_data' / 'trading_history.db'

# Eastern Time helper (mirrors orchestrator's _et_now)
def _et_now():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York"))


# ── Clamp helpers for Grok-returned targets ──────────────────────────────────
_STOP_MIN, _STOP_MAX = 0.005, 0.02       # 0.5% – 2%
_TP_MIN, _TP_MAX     = 0.015, 0.06       # 1.5% – 6%
_STRETCH_MIN, _STRETCH_MAX = 0.04, 0.10  # 4% – 10%
_MIN_CONFIDENCE = 50                       # Below this → skip trade
_MIN_POSITION = 1000                       # Don't trade if sized below $1K
_MAX_RISK_PER_TRADE_PCT = 0.01             # Risk max 1% of capital per day trade
_SOFT_MAX_GAP_PCT = 8.0                    # Above this requires explicit anti-chasing justification
_STRETCH_CUTOFF_HOUR = 11                  # Before 11:30 AM → hold for stretch
_STRETCH_CUTOFF_MINUTE = 30
_TRAILING_STOP_PCT = 0.01                  # 1% trailing stop in stretch mode


class DayTrader:
    """Grok-powered intraday trader. One pick per day, buy at open, exit by close."""

    def __init__(self, db_path=None, paper_trading=None, alerts=None, realtime_monitor=None):
        self.db_path = str(db_path or DB_PATH)
        self.paper_trading = paper_trading
        self.alerts = alerts
        self.realtime_monitor = realtime_monitor
        self.grok = GrokClient()

        # Checkpoint guards (in-memory, same pattern as orchestrator flash briefings)
        self._scan_date = None
        self._buy_date = None
        self._eod_exit_date = None
        self._enabled = True

        self._ensure_table()

    # ── DB setup ──────────────────────────────────────────────────────────────

    def _ensure_table(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS day_trade_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL UNIQUE,
                ticker TEXT,
                scan_time TEXT,
                scan_research TEXT,
                scan_confidence INTEGER,
                scan_sources INTEGER,
                gap_pct REAL,
                relative_volume REAL,
                stop_loss_pct REAL,
                take_profit_pct REAL,
                stretch_target_pct REAL,
                portfolio_risk_pct REAL,
                suggested_position_dollars REAL,
                stop_below REAL,
                first_target REAL,
                trailing_plan TEXT,
                edge_summary TEXT,
                alternatives_json TEXT,
                tp1_hit_time TEXT,
                high_water_price REAL,
                position_size_dollars REAL,
                cash_available_at_scan REAL,
                entry_trade_id INTEGER,
                entry_price REAL,
                entry_time TEXT,
                shares INTEGER,
                exit_price REAL,
                exit_time TEXT,
                exit_reason TEXT,
                pnl_dollars REAL,
                pnl_percent REAL,
                status TEXT DEFAULT 'pending',
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        for col, coltype in (
            ('gap_pct', 'REAL'),
            ('relative_volume', 'REAL'),
            ('portfolio_risk_pct', 'REAL'),
            ('suggested_position_dollars', 'REAL'),
            ('stop_below', 'REAL'),
            ('first_target', 'REAL'),
            ('trailing_plan', 'TEXT'),
            ('edge_summary', 'TEXT'),
            ('alternatives_json', 'TEXT'),
        ):
            try:
                conn.execute(f"ALTER TABLE day_trade_sessions ADD COLUMN {col} {coltype}")
            except sqlite3.OperationalError:
                pass
        conn.commit()
        conn.close()

    # ── Enable/disable ────────────────────────────────────────────────────────

    def set_enabled(self, flag: bool):
        self._enabled = flag
        logger.info(f"Day Trader {'ENABLED' if flag else 'DISABLED'}")

    # ── Position sizing ───────────────────────────────────────────────────────

    def _get_available_cash(self) -> float:
        """Get available cash (equity minus deployed positions)."""
        if not self.paper_trading:
            return 0.0
        try:
            perf = self.paper_trading.get_portfolio_performance()
            # Prefer broker cash if available, else estimate from total capital minus deployed
            broker_cash = perf.get('broker_cash')
            if broker_cash and broker_cash > 0:
                return float(broker_cash)
            # Fallback: total_capital minus open position sizes
            total = self.paper_trading.total_capital
            open_positions = self.paper_trading.get_open_positions()
            deployed = sum(float(p.get('position_size_dollars', 0) or 0) for p in open_positions)
            return max(0, total - deployed)
        except Exception as e:
            logger.warning(f"Could not fetch available cash: {e}")
            return 0.0

    def _calculate_position_size(self, confidence: int, available_cash: float,
                                 stop_pct: float, suggested_position: Optional[float] = None,
                                 portfolio_risk_pct: Optional[float] = None) -> float:
        """Size from risk, not conviction.

        Preferred path:
        - honor model-provided risk budget if present
        - cap risk to 1% of available cash
        - derive notional from stop distance

        Fallback path:
        - if stop_pct is missing/invalid, use a conservative capped notional
        """
        if confidence < _MIN_CONFIDENCE or available_cash <= 0:
            return 0.0

        risk_budget_pct = portfolio_risk_pct if portfolio_risk_pct is not None else _MAX_RISK_PER_TRADE_PCT
        try:
            risk_budget_pct = float(risk_budget_pct)
        except Exception:
            risk_budget_pct = _MAX_RISK_PER_TRADE_PCT
        risk_budget_pct = max(0.0, min(_MAX_RISK_PER_TRADE_PCT, risk_budget_pct))

        try:
            stop_pct = float(stop_pct or 0)
        except Exception:
            stop_pct = 0.0

        risk_budget_dollars = available_cash * risk_budget_pct

        risk_based_size = 0.0
        if stop_pct > 0:
            risk_based_size = risk_budget_dollars / stop_pct

        if suggested_position is not None:
            try:
                suggested_position = float(suggested_position)
            except Exception:
                suggested_position = 0.0
        else:
            suggested_position = 0.0

        if risk_based_size <= 0:
            conservative_fallback = min(available_cash * 0.10, available_cash)
            base_size = conservative_fallback
        elif suggested_position > 0:
            base_size = min(risk_based_size, suggested_position)
        else:
            base_size = risk_based_size

        return max(0.0, min(available_cash, base_size))

    # ── Live price helper ─────────────────────────────────────────────────────

    def _get_live_price(self, ticker: str) -> Optional[float]:
        """Get live price — prefer realtime stream, fallback to yfinance."""
        # Try realtime monitor first
        if self.realtime_monitor:
            try:
                price = self.realtime_monitor.get_price(ticker)
                if price and price > 0:
                    return float(price)
            except Exception:
                pass
        # Fallback: yfinance
        try:
            import yfinance as yf
            fi = yf.Ticker(ticker).fast_info
            price = fi.get('lastPrice') or fi.get('regularMarketPrice')
            if price and price > 0:
                return float(price)
        except Exception:
            pass
        return None

    # ══════════════════════════════════════════════════════════════════════════
    # CHECKPOINT 1: Pre-market Scan (7:00 AM ET)
    # ══════════════════════════════════════════════════════════════════════════

    def check_premarket_scan(self):
        """Fire once per day at/after 7:00 AM ET. Calls Grok for today's pick."""
        now = _et_now()
        today = now.strftime('%Y-%m-%d')

        # Weekday only
        if now.weekday() >= 5:
            return

        # Time gate
        if now.hour < 7:
            return

        # In-memory guard
        if self._scan_date == today:
            return

        # DB guard (survive restarts)
        try:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                "SELECT status FROM day_trade_sessions WHERE date = ?", (today,)
            ).fetchone()
            conn.close()
            if row and row[0] in ('scanned', 'bought', 'stretching', 'closed', 'skipped'):
                self._scan_date = today
                return
        except Exception:
            pass

        logger.info("🌅 Day Trader: pre-market scan firing...")
        self._scan_date = today

        try:
            result = self._run_premarket_scan(today, now)
            if result:
                logger.info(f"  ✅ Pick: {result.get('ticker')} (confidence: {result.get('confidence')}%)")
            else:
                logger.info("  ⏭️  No pick today (scan returned nothing or confidence too low)")
        except Exception as e:
            logger.error(f"  ❌ Pre-market scan failed: {e}")
            self._save_session_error(today, str(e))

    def reset_today_session(self) -> bool:
        """Delete today's session so a same-day validation scan can rerun cleanly."""
        today = _et_now().strftime('%Y-%m-%d')
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.execute("DELETE FROM day_trade_sessions WHERE date = ?", (today,))
            conn.commit()
            conn.close()
            self._scan_date = None
            self._buy_date = None
            self._eod_exit_date = None
            logger.info(f"🧹 Day Trader: reset session for {today} ({cur.rowcount} row deleted)")
            return True
        except Exception as e:
            logger.error(f"❌ Day Trader: could not reset today's session: {e}")
            return False

    def force_premarket_scan(self, reset_today: bool = True) -> Optional[Dict]:
        """Run today's Grok scan immediately, bypassing time/date guards for validation."""
        now = _et_now()
        today = now.strftime('%Y-%m-%d')

        if now.weekday() >= 5:
            logger.info("⏭️  Day Trader force scan skipped on weekend")
            return None

        if reset_today:
            self.reset_today_session()

        logger.info("🚀 Day Trader: force pre-market scan firing...")
        self._scan_date = today

        try:
            result = self._run_premarket_scan(today, now)
            if result:
                logger.info(f"  ✅ Forced pick: {result.get('ticker')} (confidence: {result.get('confidence')}%)")
            else:
                logger.info("  ⏭️  Forced scan returned no qualified pick")
            return result
        except Exception as e:
            logger.error(f"  ❌ Forced pre-market scan failed: {e}")
            self._save_session_error(today, str(e))
            return None

    def _run_premarket_scan(self, today: str, now) -> Optional[Dict]:
        """Call Grok Responses API with web + X search for today's pick."""
        available_cash = self._get_available_cash()

        system_prompt = f"""You are the HighTrade Day Trader — an intraday momentum specialist with strict risk discipline.
Your ONLY job: identify either (a) the single best high-probability catalyst-driven stock for a 9:35 AM entry or (b) declare \"NO TRADE\" if no setup meets all filters.

GOAL:
- Find a realistic 1-5% intraday edge
- Max portfolio risk per trade: 1% of capital
- Positive expectancy is more important than activity

EVALUATION STEPS (must follow internally):
1. Search web + X for TODAY's catalysts and pre-market data.
2. Build a candidate list and calculate pre-market gap % for each.
3. Rank the top 3 candidates on catalyst quality, liquidity, relative volume, technical setup, and risk/reward.
4. Apply ALL avoid filters strictly.
5. Select the best setup or return NO TRADE.

WHAT TO LOOK FOR (priority order):
1. Pre-market movers up 3-8% with fresh NEWS catalyst and volume confirmation
2. Earnings reactions with strong liquidity and sector support
3. Analyst upgrades/downgrades announced this morning
4. FDA decisions, contract wins, product launches happening TODAY
5. Sector momentum with clear leadership stock
6. X/social velocity with price confirmation

MANDATORY FILTERS:
- Average daily volume should be strong and liquid
- Relative volume should be elevated
- Setup must not be a blind chase far above structure
- If pre-market gap is >= {_SOFT_MAX_GAP_PCT:.0f}%, you must provide explicit anti-chasing justification or reject it

AVOID (strict):
- Stocks under $5
- Stocks with market cap under $1B
- Meme stocks without a real catalyst
- ADRs with limited US trading volume
- Low relative volume or poor liquidity

EXIT & RISK RULES:
- suggested_position_dollars must be sized so stop-loss risks <= 1% of capital
- stop_loss_pct must be structure-based and between 0.7% and 2.0%
- take_profit_pct and stretch_target_pct must be tied to logical levels and maintain a sensible reward/risk profile
- If no setup satisfies these rules, return NO TRADE

Use web search and X search to find TODAY's catalysts. Check pre-market prices, gap %, and volume.

Respond with ONLY valid JSON:
{{
    "ticker": "SYMBOL or NO TRADE",
    "catalyst": "Specific event driving today's move or why no trade qualifies",
    "confidence": 0-100,
    "pre_market_price": float or null,
    "gap_pct": float,
    "relative_volume": float or null,
    "expected_move_pct": float,
    "stop_loss_pct": float,
    "take_profit_pct": float,
    "stretch_target_pct": float,
    "suggested_position_dollars": float,
    "portfolio_risk_pct": float,
    "key_technical_levels": {{
        "stop_below": float or null,
        "first_target": float or null,
        "trailing_plan": "string"
    }},
    "risk": "Primary risk for this trade",
    "thesis": "2-4 sentence thesis for why this moves today or why no trade qualifies",
    "edge_summary": "Why this is not chasing and why the reward/risk is acceptable",
    "alternatives": ["brief rejected idea + reason", "brief rejected idea + reason"],
    "sources": ["list of sources/posts found"]
}}"""

        day_name = now.strftime('%A')
        user_prompt = (
            f"Today is {today} ({day_name}). Market opens in ~2.5 hours.\n"
            f"Available trading capital: ${available_cash:,.0f}. Max portfolio risk per trade: {(_MAX_RISK_PER_TRADE_PCT * 100):.1f}%.\n\n"
            "Search the web and X right now for:\n"
            "1. Stocks trending in pre-market trading (include gap % and volume)\n"
            "2. Overnight earnings results with biggest reactions\n"
            "3. Analyst calls, upgrades, downgrades issued this morning\n"
            "4. Any breaking news that creates a tradeable catalyst TODAY\n\n"
            "Internally rank candidates, apply every filter strictly, and either return your #1 qualified pick or NO TRADE. ONE decision only."
        )

        text, in_tok, out_tok = self.grok.call_with_search(
            user_prompt, system_prompt=system_prompt, temperature=0.3
        )

        if not text:
            self._save_session_error(today, "Grok returned empty response")
            return None

        # Clean JSON
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        try:
            pick = json.loads(text)
        except json.JSONDecodeError:
            self._save_session_error(today, f"JSON parse failed: {text[:200]}")
            return None

        confidence = int(pick.get('confidence', 0))
        ticker = (pick.get('ticker') or '').upper().strip()
        if not ticker:
            self._save_session_error(today, "No ticker in Grok response")
            return None

        if ticker == 'NO TRADE':
            self._update_session(today,
                                 ticker='NO TRADE',
                                 scan_time=now.strftime('%H:%M:%S'),
                                 scan_research=json.dumps(pick),
                                 scan_confidence=confidence,
                                 scan_sources=len(pick.get('sources', [])),
                                 status='skipped',
                                 error_message=pick.get('catalyst', 'No qualified setup'))
            logger.info("  ⏭️  Grok returned NO TRADE — no qualified setup today")
            return None

        try:
            gap_pct = float(pick.get('gap_pct', 0) or 0)
        except Exception:
            gap_pct = 0.0
        try:
            relative_volume = float(pick.get('relative_volume', 0) or 0)
        except Exception:
            relative_volume = 0.0

        edge_summary = str(pick.get('edge_summary', '') or '')
        tech = pick.get('key_technical_levels') or {}
        try:
            stop_below = float(tech.get('stop_below')) if tech.get('stop_below') is not None else None
        except Exception:
            stop_below = None
        try:
            first_target = float(tech.get('first_target')) if tech.get('first_target') is not None else None
        except Exception:
            first_target = None
        trailing_plan = str(tech.get('trailing_plan', '') or '')
        alternatives_json = json.dumps(pick.get('alternatives', []))
        if gap_pct >= _SOFT_MAX_GAP_PCT and 'not chasing' not in edge_summary.lower():
            self._save_session_error(today, f"Rejected {ticker}: gap {gap_pct:.1f}% without anti-chasing justification")
            self._update_session(today, status='skipped')
            logger.info(f"  ⏭️  Rejecting {ticker}: gap {gap_pct:.1f}% without anti-chasing justification")
            return None

        # Clamp exit targets
        stop = max(_STOP_MIN, min(_STOP_MAX, (pick.get('stop_loss_pct', 1.0) or 1.0) / 100))
        tp = max(_TP_MIN, min(_TP_MAX, (pick.get('take_profit_pct', 3.0) or 3.0) / 100))
        stretch = max(_STRETCH_MIN, min(_STRETCH_MAX, (pick.get('stretch_target_pct', 6.0) or 6.0) / 100))

        suggested_position = pick.get('suggested_position_dollars')
        portfolio_risk_pct = pick.get('portfolio_risk_pct')
        try:
            portfolio_risk_pct_db = float(portfolio_risk_pct) if portfolio_risk_pct is not None else _MAX_RISK_PER_TRADE_PCT
        except Exception:
            portfolio_risk_pct_db = _MAX_RISK_PER_TRADE_PCT
        portfolio_risk_pct_db = max(0.0, min(_MAX_RISK_PER_TRADE_PCT, portfolio_risk_pct_db))
        try:
            suggested_position_db = float(suggested_position) if suggested_position is not None else None
        except Exception:
            suggested_position_db = None

        # Calculate position size
        position_size = self._calculate_position_size(
            confidence,
            available_cash,
            stop,
            suggested_position=suggested_position,
            portfolio_risk_pct=portfolio_risk_pct,
        )
        status = 'scanned' if confidence >= _MIN_CONFIDENCE else 'skipped'

        if position_size < _MIN_POSITION:
            status = 'skipped'

        # Save session
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT OR REPLACE INTO day_trade_sessions
            (date, ticker, scan_time, scan_research, scan_confidence, scan_sources,
             gap_pct, relative_volume,
             stop_loss_pct, take_profit_pct, stretch_target_pct,
             portfolio_risk_pct, suggested_position_dollars,
             stop_below, first_target, trailing_plan, edge_summary, alternatives_json,
             position_size_dollars, cash_available_at_scan, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            today, ticker, now.strftime('%H:%M:%S'),
            json.dumps(pick), confidence,
            len(pick.get('sources', [])),
            gap_pct, relative_volume,
            stop, tp, stretch,
            portfolio_risk_pct_db, suggested_position_db,
            stop_below, first_target, trailing_plan, edge_summary, alternatives_json,
            round(position_size, 2), round(available_cash, 2),
            status,
        ))
        conn.commit()
        conn.close()

        # Alert to #logs-silent
        if self.alerts:
            try:
                self.alerts.send_silent_log('daytrade_scan', {
                    'ticker': ticker,
                    'confidence': confidence,
                    'thesis': pick.get('thesis', ''),
                    'catalyst': pick.get('catalyst', ''),
                    'sources': len(pick.get('sources', [])),
                    'stop_loss_pct': round(stop * 100, 1),
                    'take_profit_pct': round(tp * 100, 1),
                    'stretch_target_pct': round(stretch * 100, 1),
                    'position_size': round(position_size, 0),
                    'status': status,
                })
            except Exception as e:
                logger.warning(f"  Alert failed: {e}")

        if status == 'skipped':
            logger.info(
                f"  ⏭️  Skipping day trade: confidence={confidence}% size=${position_size:,.0f} gap={gap_pct:.1f}%"
            )
            return None

        return pick

    # ══════════════════════════════════════════════════════════════════════════
    # CHECKPOINT 2: Market Open Buy (9:35 AM ET)
    # ══════════════════════════════════════════════════════════════════════════

    def check_market_open_buy(self):
        """Fire once per day at/after 9:35 AM ET. Buys the scanned pick."""
        now = _et_now()
        today = now.strftime('%Y-%m-%d')

        if now.weekday() >= 5:
            return
        if now.hour < 9 or (now.hour == 9 and now.minute < 35):
            return
        if self._buy_date == today:
            return

        # DB guard
        try:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                "SELECT status, ticker, scan_confidence, position_size_dollars, "
                "stop_loss_pct, take_profit_pct, stretch_target_pct "
                "FROM day_trade_sessions WHERE date = ?", (today,)
            ).fetchone()
            conn.close()
        except Exception:
            row = None

        if not row:
            self._buy_date = today
            return  # No scan today

        status, ticker, confidence, position_size, stop_pct, tp_pct, stretch_pct = row

        if status != 'scanned':
            self._buy_date = today
            return  # Already bought, skipped, or errored

        self._buy_date = today
        logger.info(f"🛒 Day Trader: executing buy for {ticker} (confidence: {confidence}%)...")

        try:
            self._execute_buy(today, ticker, position_size)
        except Exception as e:
            logger.error(f"  ❌ Buy execution failed: {e}")
            self._update_session(today, status='error', error_message=str(e))

    def _execute_buy(self, today: str, ticker: str, target_size: float):
        """Buy the pick using manual_buy."""
        if not self.paper_trading:
            self._update_session(today, status='error', error_message='paper_trading not available')
            return

        price = self._get_live_price(ticker)
        if not price or price <= 0:
            self._update_session(today, status='error', error_message=f'No live price for {ticker}')
            return

        # Recalculate with live price to confirm sizing
        shares = math.floor(target_size / price)
        if shares <= 0 or (shares * price) < _MIN_POSITION:
            self._update_session(today, status='skipped',
                                 error_message=f'Position too small: {shares} shares × ${price:.2f}')
            return

        actual_size = round(shares * price, 2)

        # Load session for thesis
        try:
            conn = sqlite3.connect(self.db_path)
            research_row = conn.execute(
                "SELECT scan_research FROM day_trade_sessions WHERE date = ?", (today,)
            ).fetchone()
            conn.close()
            thesis = ''
            if research_row and research_row[0]:
                pick = json.loads(research_row[0])
                thesis = pick.get('catalyst', '') or pick.get('thesis', '')
        except Exception:
            thesis = ''

        result = self.paper_trading.manual_buy(
            ticker, shares,
            notes=f'[DAYTRADE] Grok pick — {thesis[:100]}'
        )

        if not result.get('ok'):
            self._update_session(today, status='error',
                                 error_message=result.get('message', 'manual_buy failed'))
            return

        trade_id = result.get('trade_id')
        entry_price = result.get('entry_price', price)
        now_str = _et_now().strftime('%H:%M:%S')

        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            UPDATE day_trade_sessions SET
                status = 'bought',
                entry_trade_id = ?,
                entry_price = ?,
                entry_time = ?,
                shares = ?,
                position_size_dollars = ?,
                high_water_price = ?
            WHERE date = ?
        """, (trade_id, entry_price, now_str, shares, actual_size, entry_price, today))
        conn.commit()
        conn.close()

        logger.info(
            f"  ✅ Bought {shares} × {ticker} @ ${entry_price:.2f} "
            f"= ${actual_size:,.2f} (trade #{trade_id})"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # CHECKPOINT 3: Intraday Exit Monitoring (every cycle)
    # ══════════════════════════════════════════════════════════════════════════

    def check_intraday_exits(self):
        """Check stop-loss, take-profit, and stretch target every cycle."""
        now = _et_now()
        today = now.strftime('%Y-%m-%d')

        if now.weekday() >= 5:
            return

        # Only check during market hours
        if now.hour < 9 or (now.hour == 9 and now.minute < 35):
            return
        if now.hour >= 16:
            return

        # Load active session
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM day_trade_sessions WHERE date = ?", (today,)
            ).fetchone()
            conn.close()
        except Exception:
            return

        if not row:
            return
        session = dict(row)

        if session['status'] not in ('bought', 'stretching'):
            return

        ticker = session['ticker']
        entry_price = session['entry_price']
        if not ticker or not entry_price:
            return

        price = self._get_live_price(ticker)
        if not price:
            return

        # Update high-water mark
        high_water = max(price, session.get('high_water_price') or entry_price)
        if high_water > (session.get('high_water_price') or 0):
            try:
                conn = sqlite3.connect(self.db_path)
                conn.execute(
                    "UPDATE day_trade_sessions SET high_water_price = ? WHERE date = ?",
                    (high_water, today)
                )
                conn.commit()
                conn.close()
            except Exception:
                pass

        stop_pct = session['stop_loss_pct'] or 0.01
        tp_pct = session['take_profit_pct'] or 0.03
        stretch_pct = session['stretch_target_pct'] or 0.06

        # ── Check 1: Stop-loss ────────────────────────────────────────────
        if session['status'] == 'bought':
            # Normal mode: stop from entry
            stop_price = entry_price * (1 - stop_pct)
            if price <= stop_price:
                logger.info(f"  🛑 Day Trade STOP HIT: {ticker} ${price:.2f} <= ${stop_price:.2f}")
                self._execute_sell(today, ticker, session, price, 'stop_loss')
                return

        elif session['status'] == 'stretching':
            # Stretch mode: trailing stop from high-water (floor = TP1 level)
            tp1_floor = entry_price * (1 + tp_pct)
            trailing_stop = high_water * (1 - _TRAILING_STOP_PCT)
            effective_stop = max(tp1_floor, trailing_stop)
            if price <= effective_stop:
                logger.info(
                    f"  📊 Day Trade TRAILING STOP: {ticker} ${price:.2f} "
                    f"<= ${effective_stop:.2f} (HW=${high_water:.2f})"
                )
                self._execute_sell(today, ticker, session, price, 'profit_target')
                return

        # ── Check 2: Take-profit / stretch decision ───────────────────────
        tp1_price = entry_price * (1 + tp_pct)
        stretch_price = entry_price * (1 + stretch_pct)

        if session['status'] == 'bought' and price >= tp1_price:
            # TP1 hit — check momentum (time-based decision)
            before_cutoff = (
                now.hour < _STRETCH_CUTOFF_HOUR or
                (now.hour == _STRETCH_CUTOFF_HOUR and now.minute < _STRETCH_CUTOFF_MINUTE)
            )

            if before_cutoff:
                # Early hit → strong momentum → upgrade to stretch mode
                logger.info(
                    f"  🚀 Day Trade TP1 HIT EARLY ({now.strftime('%H:%M')}): "
                    f"{ticker} ${price:.2f} >= ${tp1_price:.2f} — upgrading to STRETCH mode"
                )
                try:
                    conn = sqlite3.connect(self.db_path)
                    conn.execute("""
                        UPDATE day_trade_sessions SET
                            status = 'stretching',
                            tp1_hit_time = ?
                        WHERE date = ?
                    """, (now.strftime('%H:%M:%S'), today))
                    conn.commit()
                    conn.close()
                except Exception:
                    pass
                return
            else:
                # Late hit → sell now
                logger.info(
                    f"  💰 Day Trade TP1 HIT LATE ({now.strftime('%H:%M')}): "
                    f"{ticker} ${price:.2f} >= ${tp1_price:.2f} — selling"
                )
                self._execute_sell(today, ticker, session, price, 'profit_target')
                return

        # ── Check 3: Stretch target hit ───────────────────────────────────
        if session['status'] == 'stretching' and price >= stretch_price:
            logger.info(
                f"  🎯 Day Trade STRETCH HIT: {ticker} ${price:.2f} >= ${stretch_price:.2f}"
            )
            self._execute_sell(today, ticker, session, price, 'profit_target')
            return

    # ══════════════════════════════════════════════════════════════════════════
    # CHECKPOINT 4: EOD Exit (3:50 PM ET)
    # ══════════════════════════════════════════════════════════════════════════

    def check_eod_exit(self):
        """Hard exit at 3:50 PM ET — no overnight holds."""
        now = _et_now()
        today = now.strftime('%Y-%m-%d')

        if now.weekday() >= 5:
            return
        if now.hour < 15 or (now.hour == 15 and now.minute < 50):
            return
        if self._eod_exit_date == today:
            return

        # DB check
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM day_trade_sessions WHERE date = ?", (today,)
            ).fetchone()
            conn.close()
        except Exception:
            self._eod_exit_date = today
            return

        if not row:
            self._eod_exit_date = today
            return
        session = dict(row)

        if session['status'] not in ('bought', 'stretching'):
            self._eod_exit_date = today
            return

        self._eod_exit_date = today
        ticker = session['ticker']
        price = self._get_live_price(ticker)

        logger.info(f"  ⏰ Day Trade EOD EXIT: {ticker} @ ${price:.2f}" if price else
                     f"  ⏰ Day Trade EOD EXIT: {ticker} (price unavailable)")

        self._execute_sell(today, ticker, session, price, 'eod')

    # ── Sell execution ────────────────────────────────────────────────────────

    def _execute_sell(self, today: str, ticker: str, session: Dict,
                      price: Optional[float], reason: str):
        """Sell the day trade position."""
        if not self.paper_trading:
            self._update_session(today, status='error', error_message='paper_trading not available')
            return

        trade_id = session.get('entry_trade_id')
        entry_price = session.get('entry_price', 0)
        shares = session.get('shares', 0)

        # Map exit reason to valid trade_records values
        db_exit_reason = {
            'stop_loss': 'stop_loss',
            'profit_target': 'profit_target',
            'eod': 'manual',
            'manual': 'manual',
        }.get(reason, 'manual')

        result = self.paper_trading.manual_sell(
            ticker, trade_id=trade_id,
            price_override=price
        )

        exit_price = price or 0
        if result.get('ok'):
            exit_price = result.get('exit_price', price) or price or 0

        pnl_dollars = (exit_price - entry_price) * shares if entry_price and shares else 0
        pnl_percent = ((exit_price / entry_price) - 1) * 100 if entry_price else 0

        now_str = _et_now().strftime('%H:%M:%S')

        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            UPDATE day_trade_sessions SET
                status = 'closed',
                exit_price = ?,
                exit_time = ?,
                exit_reason = ?,
                pnl_dollars = ?,
                pnl_percent = ?
            WHERE date = ?
        """, (exit_price, now_str, reason, round(pnl_dollars, 2), round(pnl_percent, 2), today))
        conn.commit()
        conn.close()

        pnl_sign = '+' if pnl_dollars >= 0 else ''
        logger.info(
            f"  {'📈' if pnl_dollars >= 0 else '📉'} Day Trade closed: {ticker} "
            f"${entry_price:.2f} → ${exit_price:.2f} | "
            f"{pnl_sign}${pnl_dollars:,.2f} ({pnl_sign}{pnl_percent:.2f}%) [{reason}]"
        )

        # Alert to #all-highpay
        if self.alerts:
            try:
                self.alerts.send_notify('daytrade_result', {
                    'ticker': ticker,
                    'entry_price': entry_price,
                    'exit_price': exit_price,
                    'pnl_dollars': pnl_dollars,
                    'pnl_pct': pnl_percent,
                    'reason': reason,
                    'shares': shares,
                    'position_size': session.get('position_size_dollars', 0),
                    'confidence': session.get('scan_confidence', 0),
                })
            except Exception as e:
                logger.warning(f"  Alert dispatch failed: {e}")

    # ── Helper: update session ────────────────────────────────────────────────

    def _update_session(self, today: str, **kwargs):
        """Update day_trade_sessions row for today."""
        if not kwargs:
            return
        cols = ', '.join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [today]
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(f"UPDATE day_trade_sessions SET {cols} WHERE date = ?", vals)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"Session update failed: {e}")

    def _save_session_error(self, today: str, error_msg: str):
        """Create or update session with error status."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                INSERT INTO day_trade_sessions (date, status, error_message)
                VALUES (?, 'error', ?)
                ON CONFLICT(date) DO UPDATE SET
                    status = 'error', error_message = excluded.error_message
            """, (today, error_msg))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Could not save session error: {e}")

    # ── Status & history (for /daytrade command and dashboard) ────────────────

    def get_today_status(self) -> Dict:
        """Get today's day trade session status."""
        today = _et_now().strftime('%Y-%m-%d')
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM day_trade_sessions WHERE date = ?", (today,)
            ).fetchone()
            conn.close()
            if not row:
                return {'status': 'no_session', 'date': today, 'enabled': self._enabled}

            session = dict(row)
            session['enabled'] = self._enabled

            # Enrich with live price if position open
            if session['status'] in ('bought', 'stretching') and session.get('ticker'):
                price = self._get_live_price(session['ticker'])
                if price:
                    session['current_price'] = price
                    entry = session.get('entry_price', 0)
                    shares = session.get('shares', 0)
                    if entry and shares:
                        session['unrealized_pnl_dollars'] = round((price - entry) * shares, 2)
                        session['unrealized_pnl_percent'] = round(((price / entry) - 1) * 100, 2)

            return session
        except Exception as e:
            return {'status': 'error', 'error': str(e), 'enabled': self._enabled}

    def get_history(self, n: int = 10) -> List[Dict]:
        """Get last N day trade sessions."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM day_trade_sessions ORDER BY date DESC LIMIT ?", (n,)
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def get_stats(self) -> Dict:
        """Aggregate day trade statistics."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM day_trade_sessions WHERE status = 'closed' ORDER BY date DESC"
            ).fetchall()
            conn.close()
        except Exception:
            return {}

        if not rows:
            return {'total_trades': 0}

        trades = [dict(r) for r in rows]
        wins = [t for t in trades if (t.get('pnl_dollars') or 0) > 0]
        losses = [t for t in trades if (t.get('pnl_dollars') or 0) <= 0]
        total_pnl = sum(t.get('pnl_dollars', 0) or 0 for t in trades)

        # Streak
        streak = 0
        streak_type = ''
        for t in trades:
            pnl = t.get('pnl_dollars', 0) or 0
            if not streak_type:
                streak_type = 'W' if pnl > 0 else 'L'
                streak = 1
            elif (pnl > 0 and streak_type == 'W') or (pnl <= 0 and streak_type == 'L'):
                streak += 1
            else:
                break

        return {
            'total_trades': len(trades),
            'wins': len(wins),
            'losses': len(losses),
            'win_rate': round(len(wins) / len(trades) * 100, 1) if trades else 0,
            'total_pnl': round(total_pnl, 2),
            'avg_win': round(sum(t['pnl_dollars'] for t in wins) / len(wins), 2) if wins else 0,
            'avg_loss': round(sum(t['pnl_dollars'] for t in losses) / len(losses), 2) if losses else 0,
            'streak': f"{streak_type}{streak}" if streak_type else '-',
        }


# ── Standalone test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    parser = argparse.ArgumentParser(description='HighTrade Day Trader utility')
    parser.add_argument('--force-scan', action='store_true', help='Reset today and rerun the Day Trader scan immediately')
    args = parser.parse_args()

    dt = DayTrader()
    if args.force_scan:
        result = dt.force_premarket_scan(reset_today=True)
        print(json.dumps({
            'forced_scan': True,
            'result': result,
            'today_status': dt.get_today_status(),
        }, indent=2, default=str))
        raise SystemExit(0)

    print("Day Trader module loaded OK.")
    print(f"Enabled: {dt._enabled}")
    print(f"Today status: {json.dumps(dt.get_today_status(), indent=2)}")
    print(f"History: {json.dumps(dt.get_history(5), indent=2)}")
    print(f"Stats: {json.dumps(dt.get_stats(), indent=2)}")
