#!/usr/bin/env python3
"""
HighTrade Broker Agent - Autonomous Trading Decision System
Analyzes market conditions, makes trade decisions, and executes on your behalf
"""

from trading_db import get_sqlite_conn
import sqlite3
import json
import logging
import os
import hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_ET = ZoneInfo('America/New_York')
def _et_now() -> datetime:
    return datetime.now(_ET)
from paper_trading import PaperTradingEngine
from alerts import AlertSystem
from quick_money_research import QuickMoneyResearch

# Use SCRIPT_DIR to ensure we're in the correct project directory
SCRIPT_DIR = Path(__file__).parent.resolve()
DB_PATH = SCRIPT_DIR / 'trading_data' / 'trading_history.db'

logger = logging.getLogger(__name__)

# Trailing stop: exit if current_price drops more than this % below the position's peak price.
# Primary exit mechanism — replaces the old fixed entry-based stop.
# Analyst's stop_loss field is now the THESIS FLOOR (immediate exit, no gate).
TRAILING_STOP_PCT = 0.03   # 3% from peak — matches paper_trading.STOP_LOSS default

# ─── Breakout entry constants ─────────────────────────────────────────────────
# Tags where trigger = price >= target (upside breakout confirmation)
UPSIDE_TRIGGER_TAGS = {'breakout'}

# Tags exempt from risk-off entry penalty (their thesis IS the crisis)
RISK_OFF_EXEMPT_TAGS = {'breakout', 'defensive-hedge', 'macro-hedge', 'crisis-commodity'}

# Max extension above target for breakout triggers (don't chase >10% above target)
BREAKOUT_MAX_EXTENSION = 0.10

# ─── Crisis weighting: hedge / commodity entry authority ──────────────────────
# During an active crisis (DEFCON ≤ 3), these tags get a relaxed entry floor so
# the system doesn't miss a move waiting for a strict breakout confirmation.
CRISIS_HEDGE_TAGS      = {'crisis-commodity', 'defensive-hedge', 'macro-hedge'}
CRISIS_ENTRY_BUFFER_D3 = 0.08   # Allow entry up to 8% below target at DEFCON 3 + news ≥ 50
CRISIS_ENTRY_BUFFER_D2 = 0.12   # Allow entry up to 12% below target at DEFCON ≤ 2


# ─── Rebound Watchlist ────────────────────────────────────────────────────────

def _queue_rebound_watchlist(exit: dict) -> None:
    """
    Called immediately after a stop-loss exit is confirmed.
    Queues the ticker into acquisition_watchlist with source='stop_loss_rebound'
    so the researcher → analyst → verifier pipeline can find a re-entry point
    and attempt to recoup the loss.

    Entry conditions are seeded with:
    - The exit price as a soft ceiling (don't re-enter above where we got stopped)
    - A note to watch for bottoming / reversal signals
    - The loss amount so the analyst knows the recovery target

    Guards (skip rebound if any are true):
    - DEFCON ≤ 3: market conditions are still hostile; re-entry likely fails again
    - Round-trips ≥ 2 in last 30 days: ticker is churning; require manual review
    """
    ticker      = exit.get('asset_symbol', '')
    exit_price  = exit.get('current_price', 0)
    entry_price = exit.get('entry_price', 0)
    loss_pct    = exit.get('profit_loss_pct', 0) * 100      # e.g. -3.2
    loss_dollars = exit.get('profit_loss_dollars', 0)
    date_str    = datetime.now().strftime('%Y-%m-%d')

    if not ticker:
        return

    # ── Guard 1: DEFCON gate ──────────────────────────────────────────────────
    # If the market is in a hostile regime (DEFCON ≤ 3), don't auto-queue a
    # rebound — the same conditions that caused the stop-loss are still active.
    try:
        _gconn = get_sqlite_conn(str(DB_PATH), timeout=5)
        row = _gconn.execute(
            "SELECT defcon_level FROM signal_monitoring ORDER BY monitor_id DESC LIMIT 1"
        ).fetchone()
        _gconn.close()
        current_defcon = row[0] if row else 5
    except Exception:
        current_defcon = 5  # assume safe if DB unreadable

    if current_defcon <= 3:
        logger.info(
            f"  ⛔ Rebound watchlist SKIPPED for {ticker} — DEFCON {current_defcon} "
            f"(market too hostile; re-entry blocked until DEFCON ≥ 4)"
        )
        return

    # ── Guard 2: Round-trip churn check ──────────────────────────────────────
    # If this ticker has been bought and fully closed ≥ 2 times in the last
    # 30 days, it's churning. Block the auto-rebound and require manual review.
    try:
        _gconn = get_sqlite_conn(str(DB_PATH), timeout=5)
        cutoff = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
        row = _gconn.execute(
            """SELECT COUNT(*) FROM trade_records
               WHERE asset_symbol = ? AND status = 'closed' AND exit_date >= ?""",
            (ticker.upper().strip(), cutoff)
        ).fetchone()
        _gconn.close()
        recent_round_trips = row[0] if row else 0
    except Exception:
        recent_round_trips = 0

    if recent_round_trips >= 2:
        logger.warning(
            f"  ⛔ Rebound watchlist SKIPPED for {ticker} — {recent_round_trips} closed trades "
            f"in last 30 days (churn guard); manual review required before re-entry"
        )
        return

    entry_conditions = (
        f"REBOUND ENTRY — exited via stop-loss at ${exit_price:.2f} "
        f"({loss_pct:.1f}%, ${loss_dollars:,.0f}). "
        f"Original entry was ${entry_price:.2f}. "
        f"Look for bottoming pattern and reversal signals below ${exit_price:.2f}. "
        f"Target: recover the stop-loss loss before seeking new profit."
    )
    notes = (
        f"Auto-queued from stop-loss exit on {date_str}. "
        f"Loss to recover: ${abs(loss_dollars):,.0f} ({abs(loss_pct):.1f}%)"
    )

    try:
        conn = get_sqlite_conn(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS acquisition_watchlist (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                date_added          TEXT NOT NULL,
                ticker              TEXT NOT NULL,
                source              TEXT DEFAULT 'daily_briefing',
                market_regime       TEXT,
                model_confidence    REAL,
                entry_conditions    TEXT,
                biggest_risk        TEXT,
                biggest_opportunity TEXT,
                status              TEXT DEFAULT 'pending',
                notes               TEXT,
                created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(date_added, ticker)
            )
        """)
        # Use INSERT OR REPLACE so if the daily briefing already queued
        # the same ticker today, the rebound context takes precedence.
        conn.execute("""
            INSERT OR REPLACE INTO acquisition_watchlist
                (date_added, ticker, source, market_regime, model_confidence,
                 entry_conditions, biggest_risk, biggest_opportunity, status, notes)
            VALUES (?, ?, 'stop_loss_rebound', 'recovery', 0.5, ?, ?, ?, 'pending', ?)
        """, (
            date_str,
            ticker.upper().strip(),
            entry_conditions,
            f"Further downside below ${exit_price:.2f} if macro deteriorates.",
            f"Price recovers to prior entry (${entry_price:.2f}) — pipeline validates re-entry momentum.",
            notes,
        ))
        conn.commit()
        conn.close()
        logger.info(
            f"  📥 Rebound watchlist: {ticker} queued for recovery research "
            f"(loss: {loss_pct:.1f}%, ${abs(loss_dollars):,.0f})"
        )

        # Notify #logs-silent so the team sees it immediately
        try:
            from alerts import AlertSystem
            AlertSystem().send_silent_log('rebound_watchlist', {
                'ticker':       ticker,
                'loss_pct':     loss_pct,
                'loss_dollars': loss_dollars,
                'exit_price':   exit_price,
                'entry_price':  entry_price,
            })
        except Exception:
            pass  # Never let alert failure block the main flow

    except Exception as e:
        logger.warning(f"Rebound watchlist insert failed for {ticker}: {e}")


# ─── Briefing Context Retrieval (module-level) ──────────────────────────────

def get_latest_briefing_context(db_path: str = None, scope: str = 'all') -> Tuple[dict, str]:
    """
    Fetch the latest daily briefing intelligence from daily_briefings.

    Returns (briefing_dict, formatted_text_block).
    Used by broker_agent, exit_analyst, monitoring, and orchestrator.

    scope:
      'all'       — full context
      'risk'      — regime / risk / opportunity / signal quality
      'positions' — position_actions + positions_at_risk
    """
    _db = db_path or str(DB_PATH)
    briefing: dict = {}
    today = _et_now().strftime('%Y-%m-%d')
    yesterday = (_et_now() - timedelta(days=1)).strftime('%Y-%m-%d')

    try:
        conn = sqlite3.connect(_db, timeout=5)
        conn.row_factory = sqlite3.Row

        # Most recent briefing (any type) from today or yesterday
        row = conn.execute("""
            SELECT model_key, date, market_regime, regime_confidence,
                   trading_stance,
                   headline_summary, biggest_risk, biggest_opportunity,
                   signal_quality, macro_alignment, congressional_alpha,
                   portfolio_assessment, entry_conditions, defcon_forecast,
                   model_confidence, full_response_json
            FROM daily_briefings
            WHERE date IN (?, ?)
            ORDER BY created_at DESC LIMIT 1
        """, (today, yesterday)).fetchone()

        if not row:
            conn.close()
            return {}, "BRIEFING CONTEXT: No recent briefing available."

        briefing = dict(row)
        briefing_date = briefing.get('date', '')

        # Staleness guard: if >1 day old, mark it
        is_stale = briefing_date < yesterday

        # Parse full_response_json for structured fields
        full_json = {}
        try:
            raw = briefing.get('full_response_json', '')
            if raw:
                full_json = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            pass

        # Extract structured arrays from full_response_json
        briefing['position_actions'] = full_json.get('position_actions', [])
        briefing['positions_at_risk'] = full_json.get('positions_at_risk', [])
        briefing['conditionals_to_watch'] = full_json.get('conditionals_to_watch', [])
        briefing['key_themes'] = full_json.get('key_themes', [])

        # Also fetch latest reasoning briefing if the most recent was a flash
        if briefing.get('model_key') in ('morning_flash', 'midday_flash'):
            reasoning_row = conn.execute("""
                SELECT market_regime, biggest_risk, biggest_opportunity,
                       portfolio_assessment, entry_conditions, signal_quality,
                       macro_alignment, model_confidence, full_response_json
                FROM daily_briefings
                WHERE date IN (?, ?) AND model_key = 'reasoning'
                ORDER BY created_at DESC LIMIT 1
            """, (today, yesterday)).fetchone()
            if reasoning_row:
                reasoning = dict(reasoning_row)
                # Merge reasoning fields as fallbacks (flash takes precedence)
                for key in ('biggest_risk', 'biggest_opportunity', 'entry_conditions',
                            'signal_quality', 'macro_alignment', 'portfolio_assessment'):
                    if not briefing.get(key) and reasoning.get(key):
                        briefing[key] = reasoning[key]
                # Parse reasoning's position_actions as fallback
                if not briefing['position_actions']:
                    try:
                        r_json = json.loads(reasoning.get('full_response_json', '{}'))
                        briefing['position_actions'] = r_json.get('position_actions', [])
                    except (json.JSONDecodeError, TypeError):
                        pass

        conn.close()

        # Build formatted text block based on scope
        if is_stale:
            return briefing, "BRIEFING CONTEXT: Latest briefing is stale (>1 day old) — using with caution."

        lines = [f"LATEST BRIEFING ({briefing.get('model_key', '?')} — {briefing_date}):"]

        if scope in ('all', 'risk'):
            lines.append(f"  Market regime: {briefing.get('market_regime', '?')} "
                         f"(confidence: {briefing.get('regime_confidence', '?')})")
            lines.append(f"  Trading stance: {briefing.get('trading_stance', 'NORMAL')}")
            lines.append(f"  Biggest risk: {briefing.get('biggest_risk', 'N/A')}")
            lines.append(f"  Biggest opportunity: {briefing.get('biggest_opportunity', 'N/A')}")
            lines.append(f"  Signal quality: {briefing.get('signal_quality', 'N/A')}")
            if scope == 'all':
                lines.append(f"  Macro alignment: {briefing.get('macro_alignment', 'N/A')}")
                lines.append(f"  Entry conditions: {briefing.get('entry_conditions', 'N/A')}")
                lines.append(f"  DEFCON forecast: {briefing.get('defcon_forecast', 'N/A')}")
                lines.append(f"  Portfolio assessment: {briefing.get('portfolio_assessment', 'N/A')}")

        if scope in ('all', 'positions'):
            pa = briefing.get('position_actions', [])
            if pa:
                lines.append("  Position actions:")
                for a in pa:
                    if isinstance(a, dict):
                        lines.append(
                            f"    {a.get('ticker', '?')}: {a.get('action', '?')} "
                            f"(urgency: {a.get('urgency', '?')}) — {a.get('reasoning', '')}"
                        )
            par = briefing.get('positions_at_risk', [])
            if par:
                lines.append("  Positions at risk:")
                for r in par:
                    lines.append(f"    {r}" if isinstance(r, str) else f"    {r}")

        text = '\n'.join(lines)
        return briefing, text

    except Exception as e:
        logger.warning(f"Briefing context query failed: {e}")
        return {}, "BRIEFING CONTEXT: Query failed — no briefing data available."


class BrokerDecisionEngine:
    """Makes autonomous trading decisions"""

    def __init__(self):
        self.paper_trading = PaperTradingEngine()
        self.alerts = AlertSystem()
        self.quick_money = QuickMoneyResearch()
        self.decision_history = []
        self._ensure_peak_price_column()
        # Pre-exit gate: track how many times each position's stop has been vetoed
        # this session. After MAX_STOP_VETOES, the gate no longer blocks (force exit).
        self._stop_veto_count: Dict[str, int] = {}
        self._MAX_STOP_VETOES = 2

    # ── Holdings context for purchase gates ────────────────────────────────

    def _get_holdings_context(self, tickers: List[str] = None) -> Tuple[dict, str]:
        """
        Query open positions from trade_records. Returns:
          - holdings dict: {ticker: {shares, cost_basis, entry_date, unrealized_pnl_pct, ...}}
          - formatted text block suitable for injection into Gemini prompts

        If `tickers` is provided, only those are included. Otherwise all open.
        """
        holdings: dict = {}
        try:
            conn = get_sqlite_conn(str(DB_PATH), timeout=5)
            conn.row_factory = sqlite3.Row
            query = """
                SELECT asset_symbol, SUM(shares) as total_shares,
                       SUM(position_size_dollars) as total_cost,
                       AVG(entry_price) as avg_entry_price,
                       MIN(entry_date) as first_entry,
                       MAX(entry_date) as last_entry,
                       COUNT(*) as lot_count,
                       MAX(current_price) as current_price,
                       SUM(unrealized_pnl_dollars) as unrealized_pnl_dollars
                FROM trade_records
                WHERE status = 'open'
            """
            if tickers:
                placeholders = ','.join('?' for _ in tickers)
                query += f" AND asset_symbol IN ({placeholders})"
                query += " GROUP BY asset_symbol"
                rows = conn.execute(query, [t.upper() for t in tickers]).fetchall()
            else:
                query += " GROUP BY asset_symbol"
                rows = conn.execute(query).fetchall()
            conn.close()

            for r in rows:
                ticker = r['asset_symbol']
                total_cost = r['total_cost'] or 0
                avg_entry  = r['avg_entry_price'] or 0
                cur_price  = r['current_price'] or avg_entry
                unrealized = r['unrealized_pnl_dollars'] or 0
                pnl_pct    = ((cur_price - avg_entry) / avg_entry * 100) if avg_entry else 0
                holdings[ticker] = {
                    'total_shares':    r['total_shares'] or 0,
                    'total_cost':      total_cost,
                    'avg_entry_price': avg_entry,
                    'current_price':   cur_price,
                    'unrealized_pnl':  unrealized,
                    'unrealized_pct':  pnl_pct,
                    'first_entry':     r['first_entry'],
                    'last_entry':      r['last_entry'],
                    'lot_count':       r['lot_count'],
                }
        except Exception as e:
            logger.warning(f"Holdings context query failed: {e}")

        # Build text block
        if not holdings:
            text = "EXISTING HOLDINGS: None — no open positions in this ticker."
        else:
            lines = ["EXISTING HOLDINGS IN PORTFOLIO:"]
            for tkr, h in holdings.items():
                lines.append(
                    f"  {tkr}: {h['total_shares']} shares, avg entry ${h['avg_entry_price']:.2f}, "
                    f"current ${h['current_price']:.2f}, unrealized {h['unrealized_pct']:+.1f}% "
                    f"(${h['unrealized_pnl']:+,.0f}), {h['lot_count']} lot(s) since {h['first_entry']}"
                )
            lines.append(
                "  ⚠️  Consider whether adding more shares is warranted given existing exposure, "
                "or if this would create excessive concentration risk."
            )
            text = '\n'.join(lines)

        return holdings, text

    # ── Briefing context for decision gates ─────────────────────────────────

    def _get_briefing_context(self, scope: str = 'all') -> Tuple[dict, str]:
        """
        Query the latest daily briefing intelligence from daily_briefings.
        Returns:
          - briefing dict: structured fields from the most recent briefing
          - formatted text block suitable for injection into Gemini prompts

        scope options:
          'all'       — full context (for exit gates, pre-purchase gates)
          'risk'      — regime / risk / opportunity / signal quality only
          'positions' — position_actions + positions_at_risk only
        """
        return get_latest_briefing_context(str(DB_PATH), scope)

    def apply_briefing_position_actions(self) -> int:
        """
        Read position_actions from the latest briefing and apply stop/TP adjustments
        to trade_records. Only tightens stops (never loosens). Returns count of
        positions adjusted.
        """
        briefing, _ = self._get_briefing_context(scope='positions')
        actions = briefing.get('position_actions', [])
        if not actions:
            return 0

        adjusted = 0
        try:
            import yfinance as _yf
            conn = get_sqlite_conn(str(DB_PATH), timeout=5)
            conn.execute("PRAGMA journal_mode=WAL")

            for action in actions:
                if not isinstance(action, dict):
                    continue
                ticker = (action.get('ticker') or '').upper().strip()
                act = action.get('action', '')
                urgency = action.get('urgency', 'routine')
                reasoning = action.get('reasoning', '')

                if not ticker:
                    continue

                # Fetch open trades for this ticker
                trades = conn.execute("""
                    SELECT trade_id, entry_price, stop_loss, take_profit_1, current_price
                    FROM trade_records
                    WHERE asset_symbol = ? AND status = 'open'
                """, (ticker,)).fetchall()

                if not trades:
                    continue

                # Get live price for stop calculation
                try:
                    stock = _yf.Ticker(ticker)
                    live_price = stock.fast_info.get('lastPrice') or stock.info.get('currentPrice')
                except Exception:
                    live_price = None

                for trade in trades:
                    trade_id = trade[0]
                    current_stop = trade[2]  # stop_loss
                    current_tp = trade[3]    # take_profit_1
                    price = live_price or trade[4] or trade[1]  # live > current > entry

                    if act == 'tighten_stop':
                        stop_pct = action.get('adjusted_stop_pct')
                        if stop_pct is not None and price:
                            new_stop = price * (1 + stop_pct / 100)  # stop_pct is negative
                            # Only tighten: new stop must be HIGHER than existing
                            if current_stop is None or new_stop > current_stop:
                                conn.execute(
                                    "UPDATE trade_records SET stop_loss = ? WHERE trade_id = ?",
                                    (round(new_stop, 2), trade_id)
                                )
                                logger.info(
                                    f"  🔧 Briefing tightened stop: {ticker} "
                                    f"${current_stop or 0:.2f} → ${new_stop:.2f} "
                                    f"({stop_pct}%) — {reasoning}"
                                )
                                adjusted += 1

                    elif act == 'take_profit':
                        tp_pct = action.get('adjusted_tp_pct')
                        if tp_pct is not None and price:
                            new_tp = price * (1 + tp_pct / 100)
                            conn.execute(
                                "UPDATE trade_records SET take_profit_1 = ? WHERE trade_id = ?",
                                (round(new_tp, 2), trade_id)
                            )
                            logger.info(
                                f"  🎯 Briefing adjusted TP: {ticker} → ${new_tp:.2f} "
                                f"({tp_pct}%) — {reasoning}"
                            )
                            adjusted += 1

                    elif act == 'exit' and urgency == 'immediate':
                        logger.warning(
                            f"  ⚠️  Briefing recommends EXIT for {ticker} "
                            f"(urgency: immediate) — {reasoning}. "
                            f"Deferring to signal-driven exit flow."
                        )

            conn.commit()
            conn.close()

        except Exception as e:
            logger.warning(f"apply_briefing_position_actions failed: {e}")

        return adjusted

    def analyze_market_for_trades(self, defcon_level: int, signal_score: float,
                                  crisis_description: str, market_data: Dict) -> Optional[Dict]:
        """
        Analyze market conditions and decide whether to execute a trade

        Returns trade decision or None if no trade warranted
        """
        if not self._should_trade(defcon_level, signal_score):
            logger.info("❌ Trade criteria not met - skipping")
            return None

        logger.info(
            "ℹ️  Crisis basket trading disabled — deferring DEFCON buy handling "
            f"for signal_score={signal_score:.1f} / DEFCON={defcon_level} to dynamic acquisition research/conditionals"
        )
        return None

    def _run_pre_exit_gate(self, trade: Dict, current_price: float,
                           stop_price: float, loss_pct: float) -> Dict:
        """
        Pre-exit deep-dive gate for stop-loss triggers.

        Before executing a stop-loss exit, asks Gemini (balanced model) whether
        the stop-hit is a genuine breakdown or market noise (gap-down spike,
        thin pre-market, news-driven temporary dip).

        Returns:
            {"approve_exit": bool, "hold_rationale": str, "concerns": []}

        SAFETY: Any error or parse failure → approve_exit=True (fail-open).
                After _MAX_STOP_VETOES vetoes this session → force approve_exit=True.
        """
        ticker   = trade['asset_symbol']
        trade_id = trade['trade_id']

        # ── Veto cap: never block more than _MAX_STOP_VETOES times per position ──
        veto_count = self._stop_veto_count.get(trade_id, 0)
        if veto_count >= self._MAX_STOP_VETOES:
            logger.info(
                f"  🚪 Pre-exit gate [{ticker}]: max vetoes ({self._MAX_STOP_VETOES}) reached — "
                f"forcing exit"
            )
            return {"approve_exit": True, "hold_rationale": "Max vetoes reached — forced exit",
                    "concerns": []}

        # ── Pull thesis / research context ───────────────────────────────────
        thesis_text = trade.get('thesis_summary') or ''
        try:
            from trading_db import get_sqlite_conn
            from pathlib import Path
            db_path = Path(__file__).parent / 'trading_data' / 'trading_history.db'
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            row = conn.execute("""
                SELECT summary, key_risks, sector, market_cap, thesis
                FROM stock_research_library
                WHERE ticker = ?
                ORDER BY created_at DESC LIMIT 1
            """, (ticker,)).fetchone()
            if row:
                thesis_text = (
                    f"{row['thesis'] or row['summary'] or ''}\n"
                    f"Sector: {row['sector']} | Risks: {row['key_risks']}"
                )
            # Recent news mentioning this ticker
            since = (datetime.now() - timedelta(hours=24)).strftime('%Y-%m-%d')
            news_rows = conn.execute("""
                SELECT timestamp, sentiment_summary, news_score
                FROM news_signals
                WHERE DATE(timestamp) >= ? AND keyword_hits_json LIKE ?
                ORDER BY news_score DESC LIMIT 3
            """, (since, f'%{ticker}%')).fetchall()
            recent_news = [
                f"[{r['timestamp'][:16]}] {r['sentiment_summary']}"
                for r in news_rows
            ]
            conn.close()
        except Exception as e:
            logger.debug(f"  Pre-exit gate context pull failed for {ticker}: {e}")
            recent_news = []

        entry_px   = trade['entry_price']
        hold_days  = (datetime.now() - datetime.strptime(
            trade['entry_date'][:10], '%Y-%m-%d')).days if trade.get('entry_date') else '?'
        news_text  = '\n'.join(f"  • {n}" for n in recent_news) if recent_news else '  • No recent mentions'

        # Pull latest briefing intelligence for this exit decision
        _, briefing_text = self._get_briefing_context(scope='positions')

        prompt = (
            f"You are a stop-loss review gate for a paper trading system.\n"
            f"A stop-loss just triggered for {ticker}. Your job: decide if we should "
            f"HONOR the stop (exit now) or HOLD through it (this is noise).\n\n"
            f"POSITION\n"
            f"  Ticker:       {ticker}\n"
            f"  Entry price:  ${entry_px:.2f}\n"
            f"  Stop price:   ${stop_price:.2f}\n"
            f"  Current price: ${current_price:.2f}\n"
            f"  Loss so far:  {loss_pct*100:.2f}%\n"
            f"  Held:         {hold_days} day(s)\n\n"
            f"ORIGINAL THESIS\n"
            f"  {thesis_text or 'Not available'}\n\n"
            f"RECENT NEWS (last 24h)\n{news_text}\n\n"
            f"{briefing_text}\n\n"
            f"YOUR DECISION:\n"
            f"  approve_exit=true  → honor the stop, exit now (thesis has broken down, "
            f"or loss is real and growing)\n"
            f"  approve_exit=false → hold through it (noise spike, thesis intact, "
            f"expect recovery)\n\n"
            f"CRITICAL RULES:\n"
            f"  • If you have any doubt, approve the exit (stops exist for a reason)\n"
            f"  • Only veto if you are highly confident this is transient noise\n"
            f"  • You may veto at most {self._MAX_STOP_VETOES} times total for this position\n"
            f"  • This is veto #{veto_count + 1} of {self._MAX_STOP_VETOES}\n\n"
            f"Respond ONLY in this exact JSON (no other text):\n"
            f'{{\n'
            f'  "approve_exit": true,\n'
            f'  "hold_rationale": "",\n'
            f'  "concerns": ["specific concern 1", "specific concern 2"]\n'
            f'}}'
        )

        try:
            text, in_tok, out_tok = gemini_client.call(prompt=prompt, model_key='balanced', caller='broker_exit')

            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()

            result = json.loads(text.strip())
            approve = result.get("approve_exit", True)

            if not approve:
                self._stop_veto_count[trade_id] = veto_count + 1
                hold_rat = result.get("hold_rationale", "")
                logger.warning(
                    f"  🚫 Pre-exit gate VETOED stop-loss [{ticker}] "
                    f"(veto {self._stop_veto_count[trade_id]}/{self._MAX_STOP_VETOES}): "
                    f"{hold_rat[:100]}"
                )
            else:
                logger.info(
                    f"  ✅ Pre-exit gate APPROVED stop-loss exit [{ticker}]"
                )
                concerns = result.get("concerns", [])
                if concerns:
                    logger.info(f"  🔍 Exit concerns: {' | '.join(concerns[:2])}")

            return result

        except Exception as e:
            logger.warning(f"  ⚠️  Pre-exit gate failed for {ticker}: {e} — approving exit (fail-open)")
            return {"approve_exit": True, "hold_rationale": f"gate_error: {e}", "concerns": []}

    def _ensure_peak_price_column(self) -> None:
        """Add peak_price column to trade_records if not present (idempotent migration)."""
        try:
            conn = get_sqlite_conn(str(DB_PATH))
            conn.execute("ALTER TABLE trade_records ADD COLUMN peak_price REAL")
            conn.commit()
            # Seed existing open trades so they start with a valid high-watermark
            conn.execute("""
                UPDATE trade_records
                SET peak_price = MAX(COALESCE(entry_price, 0), COALESCE(current_price, 0))
                WHERE status = 'open' AND peak_price IS NULL
            """)
            conn.commit()
            conn.close()
            logger.info("Migrated trade_records: added peak_price column")
        except Exception:
            pass  # Column already exists or DB unavailable — both are fine

    def analyze_positions_for_exits(self) -> List[Dict]:
        """
        Analyze all open positions and decide which ones to exit

        Returns list of exit decisions
        """
        exit_decisions = []

        # Get all open positions
        self.paper_trading.connect()
        try:
            self.paper_trading.cursor.execute('''
            SELECT trade_id, asset_symbol, entry_price, position_size_dollars,
                   defcon_at_entry, shares, entry_date, stop_loss, take_profit_1,
                   peak_price,
                   catalyst_event, catalyst_window_end,
                   catalyst_spike_pct, catalyst_failure_pct
            FROM trade_records
            WHERE status = 'open'
            ''')

            open_trades = [dict(row) for row in self.paper_trading.cursor.fetchall()]
        finally:
            self.paper_trading.disconnect()

        # Analyze each position
        for trade in open_trades:
            current_price = self.paper_trading._get_current_price(trade['asset_symbol'])
            if not current_price or current_price <= 0:
                continue

            entry_price = trade['entry_price']
            profit_loss_pct = (current_price - entry_price) / entry_price
            profit_loss_dollars = profit_loss_pct * trade['position_size_dollars']

            # Per-position exit levels (from analyst); fall back to global constants if not set
            tp1_price     = trade.get('take_profit_1')
            thesis_floor  = trade.get('stop_loss')        # analyst's hard invalidation floor
            tp_threshold  = ((tp1_price - entry_price) / entry_price) if tp1_price else self.paper_trading.PROFIT_TARGET
            tp_src        = f"${tp1_price:.2f}" if tp1_price else f"{self.paper_trading.PROFIT_TARGET*100:.0f}% (default)"

            # Trailing stop — 3% below peak price (high watermark since entry)
            peak_price         = trade.get('peak_price') or entry_price   # fallback: entry if never updated
            trailing_stop_px   = round(peak_price * (1 - TRAILING_STOP_PCT), 4)
            peak_gain_pct      = (peak_price - entry_price) / entry_price  # how much we're up from entry to peak
            trailing_stop_src  = (
                f"trailing -3% from peak ${peak_price:.2f} "
                f"({'at entry' if peak_gain_pct < 0.001 else f'+{peak_gain_pct*100:.1f}% gain locked'})"
            )

            # ── Catalyst exit check (runs BEFORE normal stop/TP) ─────────────
            # If this position was entered on a specific event catalyst, apply
            # event-specific exit rules during the catalyst window.
            cat_event   = trade.get('catalyst_event')
            cat_end_str = trade.get('catalyst_window_end')
            cat_spike   = trade.get('catalyst_spike_pct')    # e.g. 4.0 → sell if up ≥4%
            cat_fail    = trade.get('catalyst_failure_pct')  # e.g. -2.0 → exit if down ≥2%

            if cat_event and cat_end_str:
                try:
                    cat_window_end = datetime.fromisoformat(cat_end_str[:19])
                    now = datetime.now()
                    in_window = now < cat_window_end
                    pnl_pct_for_cat = profit_loss_pct * 100  # convert to percentage

                    if in_window:
                        # Within catalyst window — apply tighter catalyst-specific rules
                        if cat_spike and pnl_pct_for_cat >= cat_spike:
                            # Spike achieved — sell into strength before "sell the news" reversal
                            decision = {
                                'trade_id':           trade['trade_id'],
                                'asset_symbol':       trade['asset_symbol'],
                                'decision_type':      'SELL_CATALYST_SPIKE',
                                'entry_price':        entry_price,
                                'current_price':      current_price,
                                'profit_loss_pct':    profit_loss_pct,
                                'profit_loss_dollars': profit_loss_dollars,
                                'reason':             f"Catalyst spike target hit: +{pnl_pct_for_cat:.1f}% ≥ {cat_spike}% | {cat_event}",
                                'confidence':         100,
                                'catalyst_event':     cat_event,
                            }
                            exit_decisions.append(decision)
                            logger.info(f"🚀 CATALYST EXIT: {trade['asset_symbol']} — spike +{pnl_pct_for_cat:.1f}% hit target ({cat_spike}%)")
                            continue

                        elif cat_fail and pnl_pct_for_cat <= cat_fail:
                            # Catalyst going wrong direction — thesis failed, exit early
                            decision = {
                                'trade_id':           trade['trade_id'],
                                'asset_symbol':       trade['asset_symbol'],
                                'decision_type':      'SELL_CATALYST_FAILED',
                                'entry_price':        entry_price,
                                'current_price':      current_price,
                                'profit_loss_pct':    profit_loss_pct,
                                'profit_loss_dollars': profit_loss_dollars,
                                'reason':             f"Catalyst thesis failed: {pnl_pct_for_cat:.1f}% ≤ {cat_fail}% during event window | {cat_event}",
                                'confidence':         100,
                                'catalyst_event':     cat_event,
                            }
                            exit_decisions.append(decision)
                            logger.warning(f"⚠️ CATALYST FAILED: {trade['asset_symbol']} — {pnl_pct_for_cat:.1f}% ≤ {cat_fail}% in window")
                            continue
                        else:
                            # Still in window, watching
                            remaining_h = (cat_window_end - now).total_seconds() / 3600
                            logger.debug(
                                f"  ⏳ {trade['asset_symbol']} catalyst window active: "
                                f"{pnl_pct_for_cat:+.1f}% | {remaining_h:.1f}h remaining | {cat_event}"
                            )
                    else:
                        # Window has expired — did the spike happen?
                        if not (cat_spike and pnl_pct_for_cat >= cat_spike):
                            # No spike materialized — event catalyst failed to drive the move
                            decision = {
                                'trade_id':           trade['trade_id'],
                                'asset_symbol':       trade['asset_symbol'],
                                'decision_type':      'SELL_CATALYST_EXPIRED',
                                'entry_price':        entry_price,
                                'current_price':      current_price,
                                'profit_loss_pct':    profit_loss_pct,
                                'profit_loss_dollars': profit_loss_dollars,
                                'reason':             f"Catalyst window expired with no spike (at {pnl_pct_for_cat:+.1f}%) | {cat_event}",
                                'confidence':         90,
                                'catalyst_event':     cat_event,
                            }
                            exit_decisions.append(decision)
                            logger.warning(
                                f"⏰ CATALYST EXPIRED: {trade['asset_symbol']} — "
                                f"window closed, {pnl_pct_for_cat:+.1f}% vs {cat_spike}% target | {cat_event}"
                            )
                            continue
                except Exception as e:
                    logger.warning(f"  ⚠️ Catalyst check failed for {trade['asset_symbol']}: {e}")
                    # Fall through to normal stop/TP logic

            # Decision 1: Hit profit target?
            if profit_loss_pct >= tp_threshold:
                decision = {
                    'trade_id': trade['trade_id'],
                    'asset_symbol': trade['asset_symbol'],
                    'decision_type': 'SELL_PROFIT_TARGET',
                    'entry_price': entry_price,
                    'current_price': current_price,
                    'profit_loss_pct': profit_loss_pct,
                    'profit_loss_dollars': profit_loss_dollars,
                    'reason': f"Hit profit target ({tp_src}): +{profit_loss_pct*100:.2f}%",
                    'confidence': 100
                }
                exit_decisions.append(decision)
                logger.info(f"📈 EXIT: {trade['asset_symbol']} - Profit target hit ({tp_src})! +{profit_loss_pct*100:.2f}%")

            # Decision 2a: Thesis floor breached — immediate exit, no gate
            # Analyst's stop_loss is a hard invalidation level: "if price drops here, the thesis is dead."
            # This fires before the trailing stop and bypasses the pre-exit gate.
            elif thesis_floor and current_price < thesis_floor:
                logger.warning(
                    f"🚨 EXIT: {trade['asset_symbol']} - Thesis floor breached "
                    f"${current_price:.2f} < floor ${thesis_floor:.2f} | "
                    f"{profit_loss_pct*100:.2f}%"
                )
                decision = {
                    'trade_id':            trade['trade_id'],
                    'asset_symbol':        trade['asset_symbol'],
                    'decision_type':       'SELL_THESIS_FLOOR',
                    'entry_price':         entry_price,
                    'current_price':       current_price,
                    'profit_loss_pct':     profit_loss_pct,
                    'profit_loss_dollars': profit_loss_dollars,
                    'reason':              f"Thesis floor breached: ${current_price:.2f} < ${thesis_floor:.2f} (analyst invalidation)",
                    'confidence':          100,
                }
                exit_decisions.append(decision)

            # Decision 2b: Trailing stop — 3% below peak price
            # Normal exit mechanism. Goes through pre-exit gate in case it's noise.
            elif current_price < trailing_stop_px:
                logger.warning(
                    f"🛑 EXIT: {trade['asset_symbol']} - Trailing stop "
                    f"${current_price:.2f} < ${trailing_stop_px:.2f} ({trailing_stop_src})"
                )

                # ── Pre-exit deep-dive gate ───────────────────────────────────
                gate = self._run_pre_exit_gate(
                    trade, current_price,
                    stop_price=trailing_stop_px,
                    loss_pct=profit_loss_pct,
                )
                if not gate.get('approve_exit', True):
                    logger.warning(
                        f"  ⏸️  Trailing stop for {trade['asset_symbol']} HELD by exit gate: "
                        f"{gate.get('hold_rationale','')[:80]}"
                    )
                    continue

                decision = {
                    'trade_id':            trade['trade_id'],
                    'asset_symbol':        trade['asset_symbol'],
                    'decision_type':       'SELL_TRAILING_STOP',
                    'entry_price':         entry_price,
                    'current_price':       current_price,
                    'profit_loss_pct':     profit_loss_pct,
                    'profit_loss_dollars': profit_loss_dollars,
                    'reason':              f"Trailing stop: {trailing_stop_src} → floor ${trailing_stop_px:.2f}",
                    'confidence':          100,
                }
                exit_decisions.append(decision)

            # Decision 3: Should we take early profit?
            elif self._should_take_early_profit(profit_loss_pct, trade):
                decision = {
                    'trade_id': trade['trade_id'],
                    'asset_symbol': trade['asset_symbol'],
                    'decision_type': 'SELL_EARLY_PROFIT',
                    'entry_price': entry_price,
                    'current_price': current_price,
                    'profit_loss_pct': profit_loss_pct,
                    'profit_loss_dollars': profit_loss_dollars,
                    'reason': f"Early profit opportunity: +{profit_loss_pct*100:.2f}%",
                    'confidence': 75
                }
                exit_decisions.append(decision)
                logger.info(f"💰 EARLY EXIT: {trade['asset_symbol']} - Taking early profit +{profit_loss_pct*100:.2f}%")

        return exit_decisions

    def get_buy_recommendations(self, top_n: int = 3) -> List[Dict]:
        """
        Get top buy recommendations for specific assets

        Analyzes which assets have been most profitable and recommends buying more
        """
        recommendations = []

        # Get asset performance
        self.paper_trading.connect()
        try:
            self.paper_trading.cursor.execute('''
            SELECT
                asset_symbol,
                COUNT(*) as total_trades,
                SUM(CASE WHEN profit_loss_dollars > 0 THEN 1 ELSE 0 END) as winners,
                AVG(profit_loss_percent) as avg_return
            FROM trade_records
            WHERE status = 'closed'
            GROUP BY asset_symbol
            ORDER BY avg_return DESC
            LIMIT ?
            ''', (top_n,))

            top_assets = [dict(row) for row in self.paper_trading.cursor.fetchall()]
        finally:
            self.paper_trading.disconnect()

        # Create recommendations
        for i, asset in enumerate(top_assets, 1):
            recommendation = {
                'rank': i,
                'asset': asset['asset_symbol'],
                'past_trades': asset['total_trades'],
                'win_rate': (asset['winners'] / asset['total_trades'] * 100) if asset['total_trades'] > 0 else 0,
                'avg_return': asset['avg_return'],
                'reason': f"Best performer: {asset['avg_return']:.2f}% avg return",
                'confidence': 60
            }
            recommendations.append(recommendation)
            logger.info(f"💡 RECOMMENDATION #{i}: {asset['asset_symbol']} - "
                       f"Avg return: {asset['avg_return']:.2f}%, Win rate: {recommendation['win_rate']:.0f}%")

        return recommendations

    def get_quick_money_opportunities(self, top_n: int = 5) -> List[Dict]:
        """
        Get quick flip opportunities for rapid trading
        
        Returns list of high-potential short-term trades
        """
        logger.info("🔍 Scanning for quick money opportunities...")
        
        try:
            opportunities = self.quick_money.research_quick_flip_opportunities()
            top_opps = opportunities[:top_n]
            
            if top_opps:
                logger.info(f"💰 Found {len(top_opps)} quick flip opportunities")
                for i, opp in enumerate(top_opps, 1):
                    logger.info(f"  #{i}: {opp['symbol']} - {opp['signal_type']} "
                              f"(Confidence: {opp['confidence']}%)")
            else:
                logger.info("No quick flip opportunities meet criteria")
                
            return top_opps
            
        except Exception as e:
            logger.error(f"Error during quick money research: {e}")
            return []

    def analyze_quick_flip_entry(self, opportunity: Dict) -> Optional[Dict]:
        """
        Analyze if we should enter a quick flip trade
        
        Returns trade decision or None
        """
        # Check if we have capital for quick flip
        available_cash = self._calculate_available_cash()
        effective_capital = self._calculate_current_exposure() + available_cash
        position_size = effective_capital * 0.10  # 10% for quick flips

        current_exposure = self._calculate_current_exposure()
        if current_exposure + position_size > effective_capital * 0.70:
            logger.warning(f"⚠️  Exposure limit - skipping quick flip {opportunity['symbol']}")
            return None
        
        # Build quick flip decision
        decision = {
            'timestamp': datetime.now().isoformat(),
            'decision_type': 'QUICK_FLIP_BUY',
            'trade_type': 'quick_flip',
            'symbol': opportunity['symbol'],
            'confidence': opportunity['confidence'],
            'signal_type': opportunity['signal_type'],
            'entry_price': opportunity['entry_price'],
            'target_price': opportunity['target_price'],
            'stop_loss': opportunity['stop_loss'],
            'position_size': position_size,
            'expected_gain_pct': opportunity['expected_gain_pct'],
            'max_hold_days': opportunity['max_hold_days'],
            'rationale': opportunity['rationale'],
            'volatility': opportunity['volatility'],
            'momentum': opportunity['momentum'],
            'rsi': opportunity['rsi']
        }
        
        logger.info(f"✅ QUICK FLIP BUY: {opportunity['symbol']} - "
                   f"Target: +{opportunity['expected_gain_pct']:.1f}%, "
                   f"Confidence: {opportunity['confidence']}%")
        
        return decision

    def _should_trade(self, defcon_level: int, signal_score: float) -> bool:
        """Determine if we should execute a trade"""
        # Relaxed: DEFCON 1-4: allow buys (we're more crisis-buy friendly); DEFCON 5: hold cash.
        if defcon_level > 4:
            return False

        # Require minimum composite signal confirmation (prevents buying on mild noise)
        if signal_score < 20:
            return False

        return True

    def _should_take_early_profit(self, profit_loss_pct: float, trade: Dict) -> bool:
        """Decide if we should take early profit before target"""
        # If up 3-4%, consider taking profit
        if 0.03 <= profit_loss_pct < 0.05:
            # But only if we're confident it will give back
            # For now, be conservative
            return False

        return False

    def _calculate_current_exposure(self) -> float:
        """Calculate total current portfolio exposure (cost basis of open positions)"""
        conn = get_sqlite_conn(str(DB_PATH))
        try:
            cursor = conn.cursor()
            cursor.execute('''
            SELECT COALESCE(SUM(position_size_dollars), 0) as total
            FROM trade_records
            WHERE status = 'open'
            ''')
            result = cursor.fetchone()
            return result[0] if result[0] else 0
        finally:
            conn.close()

    def _calculate_available_cash(self) -> float:
        """Calculate actual available cash: total_capital + realized_pnl - open_exposure.
        Accounts for realized P&L from closed trades so we don't over-size positions."""
        conn = get_sqlite_conn(str(DB_PATH))
        try:
            cursor = conn.cursor()
            # Realized P&L from closed trades
            cursor.execute('''
            SELECT COALESCE(SUM(profit_loss_dollars), 0)
            FROM trade_records
            WHERE status = 'closed'
            ''')
            realized_pnl = cursor.fetchone()[0]

            # Current open exposure (cost basis)
            cursor.execute('''
            SELECT COALESCE(SUM(position_size_dollars), 0)
            FROM trade_records
            WHERE status = 'open'
            ''')
            open_exposure = cursor.fetchone()[0]

            available = self.paper_trading.total_capital + realized_pnl - open_exposure
            return max(0, available)
        finally:
            conn.close()

    def record_decision(self, decision: Dict, executed: bool = False, result: Optional[str] = None):
        """Record a trading decision in history"""
        self.decision_history.append({
            'timestamp': datetime.now().isoformat(),
            'decision': decision,
            'executed': executed,
            'result': result
        })

        if executed:
            logger.info(f"✅ DECISION EXECUTED: {decision.get('decision_type')}")
        else:
            logger.info(f"⏭️  DECISION SKIPPED: {decision.get('decision_type')}")

    # ── Acquisition conditional checking ──────────────────────────────────────

    def _run_pre_purchase_gate(self, cond: dict, current_price: float,
                               live_state: dict) -> dict:
        """
        Run the pre-purchase AI gate before triggering an acquisition.
        Primary: grounded Gemini 3.1 Pro via REST.
        Fallback: Grok 4.1 fast reasoning.
        Returns {"approve": bool, "reason": str, "veto_reason": str, "conditions_met": list}.
        On any error, defaults to APPROVE (fail-open) so an AI outage doesn't block all trading.
        """
        import gemini_client
        import grok_client

        ticker    = cond.get('ticker', '?')
        tag       = cond.get('watch_tag') or 'untagged'
        thesis    = cond.get('thesis_summary', 'No thesis on file.')
        entry_tgt = cond.get('entry_price_target', '?')
        stop      = cond.get('stop_loss', '?')
        tp1       = cond.get('take_profit_1', '?')

        try:
            entry_conds = json.loads(cond.get('entry_conditions_json') or '[]')
        except Exception:
            entry_conds = []
        try:
            inval_conds = json.loads(cond.get('invalidation_conditions_json') or '[]')
        except Exception:
            inval_conds = []

        entry_conds_text = '\n'.join(f"  - {c}" for c in entry_conds) or '  - (none specified)'
        inval_conds_text = '\n'.join(f"  - {c}" for c in inval_conds) or '  - (none specified)'

        vix        = live_state.get('vix', 'N/A')
        defcon     = live_state.get('defcon', 'N/A')
        news_score = live_state.get('news_score', 'N/A')
        macro_score = live_state.get('macro_score', 'N/A')

        import gemini_client as _gc
        _session_ctx = _gc.market_context_block(vix=float(vix) if vix else None)

        # Analyst consensus: how many times has this ticker been recommended in 7 days?
        try:
            import sqlite3 as _sq3
            from pathlib import Path as _Path
            _cdb = _sq3.connect(str(_Path(__file__).parent / 'trading_data' / 'trading_history.db'))
            consensus_count = _cdb.execute(
                "SELECT COUNT(*) FROM conditional_tracking "
                "WHERE ticker=? AND created_at >= datetime('now','-7 days')",
                (ticker,)
            ).fetchone()[0]
            _cdb.close()
        except Exception:
            consensus_count = 0

        # Fetch existing holdings for this ticker so the gate can see our exposure
        holdings, holdings_text = self._get_holdings_context([ticker])

        # Fetch latest briefing risk assessment + trading stance
        briefing, briefing_risk_text = self._get_briefing_context(scope='risk')
        biggest_risk = briefing.get('biggest_risk', '')
        entry_conds_briefing = briefing.get('entry_conditions', '')
        market_regime = briefing.get('market_regime', 'unknown')
        regime_confidence = briefing.get('regime_confidence', '?')
        signal_quality = briefing.get('signal_quality', '')
        trading_stance = (briefing.get('trading_stance') or 'NORMAL').upper()
        if trading_stance not in ('AGGRESSIVE', 'NORMAL', 'CAUTIOUS', 'DEFENSIVE'):
            trading_stance = 'NORMAL'

        # ── Stance-dependent gate instructions ───────────────────────────
        stance_instructions = {
            'AGGRESSIVE': (
                "STANCE: AGGRESSIVE — market conditions favor new entries.\n"
                "RULES:\n"
                "  - ONLY check invalidation conditions. If any invalidation is triggered, VETO.\n"
                "  - Entry conditions are INFORMATIONAL ONLY — do NOT veto for unmet entry conditions.\n"
                "  - Briefing risk is context only — do NOT veto based on broad market caution.\n"
                "  - Still check holdings for concentration risk.\n"
            ),
            'NORMAL': (
                "STANCE: NORMAL — standard gate behavior.\n"
                "RULES:\n"
                "  - Check entry conditions: a PARTIAL pass is acceptable (majority met is fine).\n"
                "  - Check invalidation conditions: any triggered = VETO.\n"
                "  - Briefing risk is ADVISORY CONTEXT ONLY — do NOT veto based on broad market\n"
                "    caution, geopolitical warnings, or general uncertainty.\n"
                "  - Only veto on briefing risk if it DIRECTLY threatens this specific ticker's thesis.\n"
                "  - Still check holdings for concentration risk.\n"
            ),
            'CAUTIOUS': (
                "STANCE: CAUTIOUS — elevated caution, tighter filter.\n"
                "RULES:\n"
                "  - ALL entry conditions must be met — no partial credit.\n"
                "  - Check invalidation conditions: any triggered = VETO.\n"
                "  - Briefing risk can contribute to a veto ONLY if the risk is DIRECTLY RELEVANT\n"
                "    to this ticker's sector or thesis (e.g. an oil shock vetoing an energy stock).\n"
                "    Broad macro caution alone is NOT grounds for veto.\n"
                "  - Still check holdings for concentration risk.\n"
            ),
            'DEFENSIVE': (
                "STANCE: DEFENSIVE — maximum caution, very few entries should pass.\n"
                "RULES:\n"
                "  - ALL entry conditions must be met.\n"
                "  - Check invalidation conditions: any triggered = VETO.\n"
                "  - Broad macro/geopolitical risk from the briefing CAN be used as veto grounds,\n"
                "    even if not directly specific to this ticker.\n"
                "  - Still check holdings for concentration risk.\n"
            ),
        }

        prompt = (
            f"You are a pre-purchase risk gate for an automated paper trading system.\n"
            f"A conditional entry just triggered for {ticker} (watch_tag: {tag}).\n\n"
            f"{_session_ctx}\n"
            f"{holdings_text}\n\n"
            f"ORIGINAL THESIS:\n{thesis}\n\n"
            f"TRADE LEVELS:\n"
            f"  Entry target: ${entry_tgt} | Current price: ${current_price:.2f}\n"
            f"  Stop loss: ${stop} | Take profit 1: ${tp1}\n\n"
            f"ANALYST'S ENTRY CONDITIONS:\n"
            f"{entry_conds_text}\n\n"
            f"INVALIDATION CONDITIONS (if any triggered, do NOT enter):\n"
            f"{inval_conds_text}\n\n"
            f"CURRENT LIVE STATE (captured at trigger time):\n"
            f"  VIX: {vix}\n"
            f"  DEFCON: {defcon}/5\n"
            f"  News score: {news_score}/100\n"
            f"  Macro composite score: {macro_score}/100\n"
            f"  Analyst consensus (last 7d): {consensus_count} recommendation(s) for {ticker}\n\n"
            f"LATEST BRIEFING CONTEXT:\n"
            f"  Biggest risk: {biggest_risk or 'N/A'}\n"
            f"  Entry conditions (briefing): {entry_conds_briefing or 'N/A'}\n"
            f"  Market regime: {market_regime} (confidence: {regime_confidence})\n"
            f"  Signal quality: {signal_quality or 'N/A'}\n\n"
            f"{stance_instructions[trading_stance]}\n"
            f"YOUR JOB:\n"
            f"1. Evaluate each entry condition against live state — mark PASS or FAIL with reason.\n"
            f"2. Evaluate each invalidation condition — mark TRIGGERED or CLEAR.\n"
            f"3. If we ALREADY hold {ticker}, evaluate concentration risk.\n"
            f"4. Apply the stance rules above to decide: approve or veto.\n"
            f"   IMPORTANT: Follow the stance rules strictly. Do not add extra caution beyond\n"
            f"   what the stance prescribes. The stance was set by the senior strategist.\n\n"
            f"Respond ONLY in this exact JSON (no other text):\n"
            f'{{\n'
            f'  "approve": true,\n'
            f'  "conditions_met": ["condition 1: PASS/FAIL — reason", "condition 2: PASS/FAIL — reason"],\n'
            f'  "invalidations_checked": ["invalidation 1: CLEAR/TRIGGERED — reason"],\n'
            f'  "reason": "brief reason for approval (empty if vetoing)",\n'
            f'  "veto_reason": "detailed reason for veto (empty if approving)",\n'
            f'  "data_gaps": ["<data absent at trigger time that would have made this decision sharper>"] \n'
            f'}}'
        )

        def _parse_gate_json(text: str) -> dict:
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()
            if "<think>" in text:
                parts = text.split("</think>")
                if len(parts) > 1:
                    text = parts[-1].strip()
            result = json.loads(text.strip())
            result['_stance_applied'] = trading_stance
            return result

        gate_attempts = []

        # Primary: SONNET 4.6 via REST (preferred for stability/cost). Try sonnet-4.6 first, then gemini-2.5-pro, then gemini-3.1-pro-preview as grounded fallback.
        try:
            text, in_tok, out_tok = gemini_client.call(
                prompt=prompt,
                model_id='sonnet-4.6',
                caller='broker_gate',
            )
            if text:
                result = _parse_gate_json(text)
                result['_gate_model'] = 'sonnet-4.6'
                result['_gate_provider'] = 'sonnet'
                result['_gate_tokens'] = {'input': in_tok, 'output': out_tok}
                return result
            gate_attempts.append('sonnet-4.6: empty response')
        except json.JSONDecodeError as e:
            gate_attempts.append(f'sonnet-4.6: json parse failed ({e})')
        except Exception as e:
            gate_attempts.append(f'sonnet-4.6: {e}')

        # Fallback: Gemini 2.5 Pro.
        try:
            text, in_tok, out_tok = gemini_client.call(
                prompt=prompt,
                model_id='gemini-2.5-pro',
                caller='broker_gate',
            )
            if text:
                result = _parse_gate_json(text)
                result['_gate_model'] = 'gemini-2.5-pro'
                result['_gate_provider'] = 'google'
                result['_gate_tokens'] = {'input': in_tok, 'output': out_tok}
                result['_gate_fallback_used'] = True
                result['_gate_attempts'] = gate_attempts
                return result
            gate_attempts.append('gemini-2.5-pro: empty response')
        except json.JSONDecodeError as e:
            gate_attempts.append(f'gemini-2.5-pro: json parse failed ({e})')
        except Exception as e:
            gate_attempts.append(f'gemini-2.5-pro: {e}')

        # Fallback: Gemini 3.1 Pro via REST with Google Search grounding enabled.
        _orig_grounding = os.environ.get('GEMINI_ENABLE_GOOGLE_SEARCH')
        try:
            os.environ['GEMINI_ENABLE_GOOGLE_SEARCH'] = '1'
            text, in_tok, out_tok = gemini_client.call(
                prompt=prompt,
                model_key='reasoning',
                model_id='gemini-3.1-pro-preview',
                caller='broker_gate',
            )
            if text:
                result = _parse_gate_json(text)
                result['_gate_model'] = 'gemini-3.1-pro-preview'
                result['_gate_provider'] = 'google-rest-grounded'
                result['_gate_tokens'] = {'input': in_tok, 'output': out_tok}
                result['_gate_attempts'] = gate_attempts
                return result
            gate_attempts.append('gemini-3.1-pro-preview: empty response')
        except json.JSONDecodeError as e:
            gate_attempts.append(f'gemini-3.1-pro-preview: json parse failed ({e})')
        except Exception as e:
            gate_attempts.append(f'gemini-3.1-pro-preview: {e}')
        finally:
            if _orig_grounding is None:
                os.environ.pop('GEMINI_ENABLE_GOOGLE_SEARCH', None)
            else:
                os.environ['GEMINI_ENABLE_GOOGLE_SEARCH'] = _orig_grounding

        logger.warning(
            f"  ⚠️  Pre-purchase gate AI stack failed for {ticker}: {' | '.join(gate_attempts)} — defaulting to APPROVE"
        )
        return {
            "approve": False,
            "reason": "gate error — fail-closed (AI stack failure)",
            "veto_reason": "AI gate failure - vetoing by policy",
            "conditions_met": [],
            "_stance_applied": trading_stance,
            "_gate_attempts": gate_attempts,
        }

    def check_acquisition_conditionals(self, live_state: dict = None) -> List[Dict]:
        """
        Check all 'active' conditionals in conditional_tracking.

        For each conditional:
          1. Fetch current live price via yfinance
          2. If price <= entry_price_target → run pre-purchase Pro gate
          3. If gate approves → mark triggered, add to results
          4. If gate vetoes → leave as active (retry next cycle)
          5. If time_horizon_days exceeded → expire the conditional

        live_state: optional dict with {defcon, news_score, macro_score} from orchestrator.
        """
        from trading_db import get_sqlite_conn
        import yfinance as yf
        from pathlib import Path

        live_state = live_state or {}
        db_path = Path(__file__).parent / 'trading_data' / 'trading_history.db'
        triggered = []
        triggered_tickers = set()  # Prevent duplicate triggers for same ticker

        # Fetch live VIX once for all conditionals this cycle
        try:
            vix_hist = yf.Ticker('^VIX').history(period='1d')
            live_state.setdefault('vix', float(vix_hist['Close'].iloc[-1]) if len(vix_hist) > 0 else 'N/A')
        except Exception:
            live_state.setdefault('vix', 'N/A')

        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            # Ensure gate_vetoed_until column exists (added to prevent re-running gate
            # every cycle for perpetually-triggered but vetoed conditionals)
            try:
                conn.execute("ALTER TABLE conditional_tracking ADD COLUMN gate_vetoed_until TEXT")
                conn.commit()
            except Exception:
                pass  # column already exists
            cursor = conn.execute("""
                SELECT id, ticker, date_created, entry_price_target,
                       stop_loss, take_profit_1, take_profit_2,
                       position_size_pct, time_horizon_days,
                       thesis_summary, research_confidence,
                       entry_conditions_json, invalidation_conditions_json,
                       watch_tag, watch_tag_rationale, gate_vetoed_until,
                       source
                FROM conditional_tracking
                WHERE status = 'active'
                ORDER BY research_confidence DESC
            """)
            actives = [dict(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.error(f"conditional_tracking query failed: {e}")
            return []

        now = datetime.now()

        for cond in actives:
            ticker      = cond['ticker']
            cond_id     = cond['id']
            entry_target = cond.get('entry_price_target')
            horizon_days = cond.get('time_horizon_days') or 30

            # Skip gate if still in veto cooldown (prevents re-running gate every cycle
            # for conditionals whose price is perpetually triggered but gate vetoes entry)
            vetoed_until_str = cond.get('gate_vetoed_until')
            if vetoed_until_str:
                try:
                    vetoed_until = datetime.fromisoformat(vetoed_until_str)
                    if now < vetoed_until:
                        logger.debug(f"  ⏩ {ticker} gate veto cooldown active until {vetoed_until_str[:16]} — skipping")
                        continue
                except Exception:
                    pass

            # Check expiry
            try:
                date_created = datetime.strptime(cond['date_created'], '%Y-%m-%d')
                if (now - date_created).days > horizon_days:
                    conn.execute(
                        "UPDATE conditional_tracking SET status='expired', updated_at=? WHERE id=?",
                        (now.isoformat(), cond_id)
                    )
                    conn.commit()
                    logger.info(f"  ⏰ {ticker} conditional expired (>{horizon_days}d)")
                    continue
            except Exception:
                pass

            # Get current price
            try:
                stock = yf.Ticker(ticker)
                hist = stock.history(period='1d')
                current_price = float(hist['Close'].iloc[-1]) if len(hist) > 0 else None
            except Exception as e:
                logger.warning(f"  ⚠️  Price fetch failed for {ticker}: {e}")
                continue

            if not current_price or not entry_target:
                continue

            # ── Briefing stance check: DEFENSIVE requires extra price discount ──
            effective_target = entry_target
            watch_tag = (cond.get('watch_tag') or 'untagged').lower()
            try:
                briefing_ctx, _ = self._get_briefing_context(scope='risk')
                _stance = (briefing_ctx.get('trading_stance') or 'NORMAL').upper()
                if _stance == 'DEFENSIVE' and watch_tag not in RISK_OFF_EXEMPT_TAGS:
                    effective_target = entry_target * 0.98  # Require 2% extra discount
                    logger.info(
                        f"  ⚠️  {ticker}: DEFENSIVE stance — "
                        f"tightening entry ${entry_target:.2f} → ${effective_target:.2f}"
                    )
                elif _stance == 'DEFENSIVE' and watch_tag in RISK_OFF_EXEMPT_TAGS:
                    logger.info(
                        f"  ℹ️  {ticker}: DEFENSIVE stance but [{watch_tag}] exempt from penalty"
                    )
            except Exception:
                pass

            logger.debug(f"  📊 {ticker}: current=${current_price:.2f}, target=${effective_target:.2f}")

            # Trigger check: direction depends on watch_tag
            if watch_tag in UPSIDE_TRIGGER_TAGS:
                # Breakout: trigger when price confirms above target (capped at 10% extension)
                max_price = entry_target * (1 + BREAKOUT_MAX_EXTENSION)
                price_triggered = current_price >= effective_target and current_price <= max_price

                # ── Crisis authority: relax entry floor for hedge/commodity tags ──
                # During an active crisis (DEFCON ≤ 3, elevated news), don't require
                # a strict breakout confirmation — the crisis IS the catalyst.
                if not price_triggered and watch_tag in CRISIS_HEDGE_TAGS:
                    _defcon = int(live_state.get('defcon', 5))
                    _news   = float(live_state.get('news_score', 0))
                    if _defcon <= 2:
                        crisis_floor = effective_target * (1 - CRISIS_ENTRY_BUFFER_D2)
                    elif _defcon <= 3 and _news >= 50:
                        crisis_floor = effective_target * (1 - CRISIS_ENTRY_BUFFER_D3)
                    else:
                        crisis_floor = None

                    if crisis_floor and current_price >= crisis_floor and current_price <= max_price:
                        price_triggered = True
                        logger.info(
                            f"  🚨 {ticker}: CRISIS ENTRY — DEFCON {_defcon}, "
                            f"news_score {_news:.0f} — entering at ${current_price:.2f} "
                            f"(target ${effective_target:.2f}, crisis floor ${crisis_floor:.2f})"
                        )
            else:
                # Pullback/dip entries: trigger when price drops to or below target
                price_triggered = current_price <= effective_target

            if price_triggered and ticker not in triggered_tickers:
                # Calculate position size using actual available cash (accounts for realized P&L)
                available_cash = self._calculate_available_cash()
                raw_pct        = float(cond.get('position_size_pct') or 0.05)
                confidence     = float(cond.get('research_confidence') or 0.5)
                # Hound picks are speculative — cap at 10% of cash regardless of analyst suggestion
                MAX_PCT = 0.10 if cond.get('source') == 'grok_hound_auto' else 0.20
                # Formulaic sizing: cash * confidence * analyst_size_pct, capped by source
                effective_pct  = min(raw_pct * confidence, MAX_PCT)
                position_dollars = available_cash * effective_pct

                # Wind-down sizing reduction: 50% during gradual de-escalation
                if live_state.get('is_winding_down', False):
                    position_dollars = position_dollars * 0.50
                    logger.info(f"  🔄 {ticker}: wind-down active — position size halved to ${position_dollars:,.0f}")

                if position_dollars < 100:
                    logger.warning(f"  ⚠️  {ticker} position too small (${position_dollars:.0f}) — skipping")
                    continue

                # Exposure guard
                if self._calculate_current_exposure() + position_dollars > self.paper_trading.total_capital * 0.60:
                    logger.warning(f"  ⚠️  {ticker} would breach 60% exposure cap — skipping")
                    continue

                # ── PRE-PURCHASE GATE: Gemini 3 Pro live conditions check ──────
                if watch_tag in UPSIDE_TRIGGER_TAGS:
                    trigger_desc = f"${current_price:.2f} >= ${entry_target:.2f} (breakout confirmed)"
                else:
                    trigger_desc = f"${current_price:.2f} <= ${entry_target:.2f}"
                logger.info(
                    f"  🎯 {ticker} [{watch_tag}] price triggered: "
                    f"{trigger_desc} — running Pro gate..."
                )
                gate = self._run_pre_purchase_gate(cond, current_price, live_state)

                # Re-check conditional status after gate call (may take 15-30s).
                # Another thread (WebSocket dispatch or cycle) may have already
                # processed this conditional while the gate was running.
                try:
                    fresh = conn.execute(
                        "SELECT status FROM conditional_tracking WHERE id=?", (cond_id,)
                    ).fetchone()
                    if fresh and fresh['status'] != 'active':
                        logger.info(f"  ⏩ {ticker} already processed by another thread (status={fresh['status']}) — skipping")
                        continue
                except Exception:
                    pass

                if not gate.get('approve', True):
                    veto = gate.get('veto_reason', 'unspecified')
                    _gate_stance = gate.get('_stance_applied', '?')
                    logger.warning(f"  🚫 {ticker} VETOED by pre-purchase gate [{_gate_stance}]: {veto}")
                    # Set 2-hour cooldown so we don't re-run the gate every 15-min cycle
                    # while the price remains triggered but conditions haven't changed
                    _veto_until = (now + timedelta(hours=2)).isoformat()
                    try:
                        conn.execute(
                            "UPDATE conditional_tracking SET gate_vetoed_until=?, updated_at=? WHERE id=?",
                            (_veto_until, now.isoformat(), cond_id)
                        )
                        conn.commit()
                    except Exception:
                        pass
                    continue

                _gate_stance = gate.get('_stance_applied', '?')
                logger.info(
                    f"  ✅ {ticker} gate APPROVED [{_gate_stance}]: {gate.get('reason', 'conditions met')}"
                )
                gate_gaps = gate.get('data_gaps', [])
                if gate_gaps:
                    logger.info(f"  🔍 Gate data gaps ({ticker}): {' | '.join(gate_gaps)}")
                    # Persist gate data_gaps into conditional_tracking (merge with existing)
                    try:
                        existing_row = conn.execute(
                            "SELECT data_gaps_json FROM conditional_tracking WHERE id=?", (cond_id,)
                        ).fetchone()
                        existing_gaps = json.loads((existing_row or {}).get('data_gaps_json') or '[]') \
                            if existing_row else []
                        seen = {g.lower().strip() for g in existing_gaps}
                        merged_gaps = list(existing_gaps)
                        for g in gate_gaps:
                            if g.lower().strip() not in seen:
                                merged_gaps.append(g)
                                seen.add(g.lower().strip())
                        merged_gaps = merged_gaps[-20:]
                        conn.execute(
                            "UPDATE conditional_tracking SET data_gaps_json=?, updated_at=? WHERE id=?",
                            (json.dumps(merged_gaps), now.isoformat(), cond_id)
                        )
                    except Exception as _ge:
                        logger.warning(f"  ⚠️  Failed to persist gate data_gaps for {ticker}: {_ge}")

                decision = {
                    'timestamp':      now.isoformat(),
                    'decision_type':  'ACQUISITION_CONDITIONAL',
                    'source':         'conditional_tracking',
                    'conditional_id': cond_id,
                    'ticker':         ticker,
                    'watch_tag':      watch_tag,
                    'current_price':  current_price,
                    'entry_target':   entry_target,
                    'stop_loss':      cond.get('stop_loss'),
                    'take_profit_1':  cond.get('take_profit_1'),
                    'take_profit_2':  cond.get('take_profit_2'),
                    'position_size':  position_dollars,
                    'position_size_pct': effective_pct,
                    'confidence':     confidence,
                    'thesis':         cond.get('thesis_summary', ''),
                    'entry_conditions': json.loads(cond.get('entry_conditions_json') or '[]'),
                    'gate_conditions_met': gate.get('conditions_met', []),
                }
                triggered.append(decision)
                triggered_tickers.add(ticker)  # Only one conditional per ticker per cycle

                # Mark as triggered in DB
                conn.execute(
                    "UPDATE conditional_tracking SET status='triggered', updated_at=? WHERE id=?",
                    (now.isoformat(), cond_id)
                )
                # Mirror triggered status to ALL non-archived watchlist rows for this ticker
                conn.execute(
                    """UPDATE acquisition_watchlist SET status='triggered'
                       WHERE UPPER(ticker)=UPPER(?) AND status NOT IN ('archived','triggered')""",
                    (ticker,)
                )
                conn.commit()

        conn.close()
        return triggered


class BrokerNotificationEngine:
    """Handles notifications and tips for the user"""

    def __init__(self):
        self.alerts = AlertSystem()
        self.trade_engine = PaperTradingEngine()

    def send_buy_notification(self, decision: Dict):
        """Notify user about a buy decision"""
        message = f"""
🎯 BROKER ACTION: BUY SIGNAL
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Decision: Autonomous Buy Executed
Time: {decision['timestamp']}

Crisis Type: {decision['crisis_type']}
DEFCON Level: {decision['defcon_level']}/5
Signal Score: {decision['signal_score']:.1f}/100
Confidence: {decision['confidence']}%

Assets Purchased:
  🔹 Primary (50%): {decision['assets']['primary']}
  🔹 Secondary (30%): {decision['assets']['secondary']}
  🔹 Tertiary (20%): {decision['assets']['tertiary']}

Position Size: ${decision['position_size']:,.0f}
VIX Level: {decision['vix']:.1f}

Rationale: {decision['rationale']}

Exit Strategy:
  ✓ Profit Target: +5%
  ✓ Stop Loss: -3%
  ✓ DEFCON Revert: Exit all

Your broker made this decision on your behalf.
Monitor portfolio: python3 trading_cli.py status
"""
        self.alerts.send_defcon_alert(
            defcon_level=decision['defcon_level'],
            signal_score=decision['signal_score'],
            details=message
        )
        logger.info("📨 Buy notification sent")

    def send_sell_notification(self, decision: Dict):
        """Notify user about a sell decision via proper position_closed event."""
        self.alerts.send_notify('position_closed', {
            'ticker':              decision.get('asset_symbol', '?'),
            'reason':              decision.get('reason', 'manual'),
            'decision_type':       decision.get('decision_type', ''),
            'entry_price':         decision.get('entry_price', 0),
            'exit_price':          decision.get('current_price', 0),
            'profit_loss_dollars': decision.get('profit_loss_dollars', 0),
            'profit_loss_pct':     decision.get('profit_loss_pct', 0),
            'shares':              decision.get('shares', 0),
            'holding_hours':       decision.get('holding_hours'),
        })
        logger.info(f"📨 Sell notification sent: {decision.get('asset_symbol')} ({decision.get('reason')})")

    def send_tip(self, tip_type: str, content: str):
        """Send trading tips to user"""
        tips_message = f"""
💡 BROKER TIP
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Type: {tip_type}
Time: {datetime.now().isoformat()}

{content}

Questions? Check the documentation:
  • PAPER_TRADING_GUIDE.md
  • trading_cli.py status
"""
        logger.info(f"💡 Tip: {tip_type}")
        logger.info(tips_message)


class AutonomousBroker:
    """Main autonomous broker that makes and executes trades"""

    def __init__(self, auto_execute: bool = True, max_daily_trades: int = 5,
                 broker_mode: str = 'full_auto'):
        self.decision_engine = BrokerDecisionEngine()
        self.notification_engine = BrokerNotificationEngine()
        self.auto_execute = auto_execute
        self.broker_mode = broker_mode
        self.max_daily_trades = max_daily_trades
        self.trades_executed_today = 0
        self.last_reset = _et_now().date()     # ET date — resets on ET calendar day

    def _ensure_notification_log_table(self) -> None:
        """Create a tiny persistence table used to suppress duplicate Slack alerts."""
        conn = get_sqlite_conn(str(DB_PATH), timeout=5)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS notification_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    ticker TEXT,
                    conditional_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def _acquisition_alert_key(self, decision: Dict, executed: bool) -> str:
        """Build a stable key for one logical acquisition notification."""
        parts = [
            'acquisition_alert',
            'executed' if executed else 'triggered',
            str(decision.get('conditional_id') or ''),
            str(decision.get('ticker') or ''),
            str(decision.get('current_price') or ''),
            str(decision.get('position_size') or ''),
        ]
        digest = hashlib.sha1('|'.join(parts).encode('utf-8')).hexdigest()[:20]
        return f"acq:{digest}"

    def _mark_acquisition_alert_sent(self, decision: Dict, executed: bool) -> bool:
        """Return True only the first time a logical acquisition alert is seen."""
        self._ensure_notification_log_table()
        event_key = self._acquisition_alert_key(decision, executed)
        conn = get_sqlite_conn(str(DB_PATH), timeout=5)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                INSERT INTO notification_log (event_key, event_type, ticker, conditional_id)
                VALUES (?, ?, ?, ?)
                """,
                (
                    event_key,
                    'acquisition_executed' if executed else 'acquisition_triggered',
                    decision.get('ticker'),
                    decision.get('conditional_id'),
                )
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
        finally:
            conn.close()

    def process_market_conditions(self, defcon_level: int, signal_score: float,
                                 crisis_description: str, market_data: Dict) -> bool:
        """
        Process current market conditions and make autonomous trading decisions

        Returns True if a trade was executed
        """
        # Reset daily counter on ET calendar day rollover
        if _et_now().date() > self.last_reset:
            self.trades_executed_today = 0
            self.last_reset = _et_now().date()

        # Check if we can make more trades today
        if self.trades_executed_today >= self.max_daily_trades:
            logger.warning(f"⚠️  Daily trade limit ({self.max_daily_trades}) reached")
            return False

        # Make trade decision
        trade_decision = self.decision_engine.analyze_market_for_trades(
            defcon_level, signal_score, crisis_description, market_data
        )

        if not trade_decision:
            return False

        # Execute if auto_execute enabled
        if self.auto_execute:
            logger.info("🤖 BROKER: Executing autonomous buy...")

            # ── Holdings-aware check: skip tickers we already hold at full size ──
            # The gate doesn't hard-block — it adjusts. If we already hold all 3
            # tickers from a recent buy, there's no new exposure to add.
            existing = trade_decision.get('existing_holdings', {})
            planned_tickers = [
                trade_decision['assets']['primary'],
                trade_decision['assets']['secondary'],
                trade_decision['assets']['tertiary'],
            ]
            already_held = [t for t in planned_tickers if t in existing]
            if len(already_held) == len(planned_tickers):
                total_existing = sum(existing[t]['total_cost'] for t in already_held)
                logger.warning(
                    f"  📋 All {len(already_held)} recommended tickers already held "
                    f"(${total_existing:,.0f} exposure) — passing to AI gate for review"
                )
                # Run a quick Gemini gate to decide if adding more is warranted
                import gemini_client
                holdings_text = trade_decision.get('existing_holdings_text', '')
                gate_prompt = (
                    f"You are a portfolio risk gate for an automated paper trading system.\n"
                    f"The broker is considering adding to already-held tickers:\n\n"
                    f"{holdings_text}\n\n"
                    f"PROPOSED PURCHASE:\n"
                    f"  Tickers: {', '.join(planned_tickers)}\n"
                    f"  New position size: ${trade_decision['position_size']:,.0f}\n"
                    f"  Crisis type: {trade_decision['crisis_type']}\n"
                    f"  DEFCON: {trade_decision['defcon_level']}/5\n"
                    f"  Signal score: {trade_decision['signal_score']:.1f}/100\n"
                    f"  Rationale: {trade_decision['rationale']}\n\n"
                    f"Should the system add MORE shares to these existing positions?\n"
                    f"Consider: Is this a genuine new signal (averaging down into confirmed weakness)\n"
                    f"or a redundant trigger (same conditions, same day, duplicate buy)?\n\n"
                    f'Respond ONLY in this exact JSON:\n'
                    f'{{"approve": true/false, "reason": "brief explanation"}}'
                )
                try:
                    gate_text, _, _ = gemini_client.call(
                        prompt=gate_prompt, model_key='balanced', caller='broker_gate'
                    )
                    if gate_text:
                        if "```" in gate_text:
                            gate_text = gate_text.split("```json")[-1].split("```")[0].strip() if "```json" in gate_text else gate_text.split("```")[1].split("```")[0].strip()
                        gate_result = json.loads(gate_text.strip())
                        if not gate_result.get('approve', True):
                            logger.info(f"  🚫 Holdings gate VETOED additional buy: {gate_result.get('reason', 'no reason')}")
                            self.decision_engine.record_decision(trade_decision, executed=False, result="VETOED_HOLDINGS")
                            return False
                        logger.info(f"  ✅ Holdings gate APPROVED additional buy: {gate_result.get('reason', '')}")
                except Exception as e:
                    logger.warning(f"  ⚠️  Holdings gate failed ({e}) — proceeding with buy (fail-open)")

            logger.info("ℹ️  Legacy DEFCON package execution removed — skipping autonomous crisis basket buy")
            self.decision_engine.record_decision(trade_decision, executed=False, result="LEGACY_BASKET_REMOVED")
            return False
        else:
            self.decision_engine.record_decision(trade_decision, executed=False)
            logger.info("ℹ️  Trade decision ready (auto_execute disabled)")

        return False

    def process_exits(self) -> int:
        """
        Process all open positions and execute exits if conditions met

        Returns number of exits executed
        """
        exits_executed = 0

        exit_decisions = self.decision_engine.analyze_positions_for_exits()

        for exit in exit_decisions:
            if self.auto_execute:
                logger.info(f"🤖 BROKER: Executing autonomous sell ({exit['asset_symbol']})...")

                # Map decision type to valid exit_reason
                # Valid reasons: profit_target, stop_loss, manual, invalidation
                _reason_map = {
                    'SELL_PROFIT_TARGET':    'profit_target',
                    'SELL_STOP_LOSS':        'stop_loss',       # legacy (kept for safety)
                    'SELL_TRAILING_STOP':    'stop_loss',       # -3% from peak
                    'SELL_THESIS_FLOOR':     'invalidation',    # analyst hard floor breached
                    'SELL_EARLY_PROFIT':     'profit_target',
                    'SELL_MANUAL':           'manual',
                    'SELL_TIME_LIMIT':       'manual',
                    'SELL_DEFCON_REVERT':    'invalidation',    # market regime forced exit
                    'SELL_CATALYST_SPIKE':   'profit_target',   # sold into strength
                    'SELL_CATALYST_FAILED':  'invalidation',    # event went wrong direction
                    'SELL_CATALYST_EXPIRED': 'invalidation',    # window closed, no move
                }
                exit_reason = _reason_map.get(exit['decision_type'], 'manual')

                # Execute the exit
                success = self.decision_engine.paper_trading.exit_position(
                    exit['trade_id'],
                    exit_reason,
                    exit['current_price']
                )

                if success:
                    exits_executed += 1
                    self.notification_engine.send_sell_notification(exit)
                    self.decision_engine.record_decision(exit, executed=True, result="SOLD")

                    # ── Rebound watchlist: queue stop-loss tickers for recovery research ──
                    if exit_reason == 'stop_loss':
                        _queue_rebound_watchlist(exit)

        return exits_executed

    def process_acquisition_conditionals(self, live_state: dict = None) -> int:
        """
        Check all active acquisition conditionals and execute entries that have
        been triggered (current price <= entry_price_target).

        Semi-auto mode: notifies via Slack, does NOT execute (user must /buy).
        Full-auto mode: executes and notifies.

        Guards against duplicate positions in the same ticker.
        Runs Gemini 3 Pro pre-purchase gate on every trigger before executing.

        live_state: optional dict with {defcon, news_score, macro_score} from orchestrator.
        Returns number of conditional entries executed (or notified in semi_auto).
        """
        logger.info("🎯 Broker: checking acquisition conditionals...")
        triggered = self.decision_engine.check_acquisition_conditionals(live_state=live_state or {})

        if not triggered:
            logger.info("  📭 No conditionals triggered this cycle")
            return 0

        # Get currently open tickers to prevent duplicates
        open_positions = self.decision_engine.paper_trading.get_open_positions()
        open_tickers = {p.get('asset_symbol') or p.get('ticker', '') for p in open_positions}

        executed_count = 0
        for decision in triggered:
            ticker        = decision['ticker']
            position_size = decision['position_size']
            entry_price   = decision['current_price']

            # GUARD: Skip buy — but use fresh analyst levels to update the open position's exit strategy
            if ticker in open_tickers:
                stop_new = decision.get('stop_loss')
                tp1_new  = decision.get('take_profit_1')
                tp2_new  = decision.get('take_profit_2')
                import sqlite3 as _sq3
                try:
                    _db = Path(__file__).parent / 'trading_data' / 'trading_history.db'
                    _conn = _sq3.connect(str(_db))
                    _conn.row_factory = _sq3.Row
                    row = _conn.execute(
                        "SELECT trade_id, stop_loss, take_profit_1 FROM trade_records WHERE asset_symbol=? AND status='open' LIMIT 1",
                        (ticker,)
                    ).fetchone()
                    if row and (stop_new or tp1_new):
                        _tid, stop_old, tp1_old = row['trade_id'], row['stop_loss'], row['take_profit_1']
                        _conn.execute(
                            "UPDATE trade_records SET stop_loss=?, take_profit_1=?, take_profit_2=? WHERE trade_id=?",
                            (stop_new, tp1_new, tp2_new, _tid)
                        )
                        logger.info(
                            f"  🔄 {ticker} exit levels updated (re-analysis) — "
                            f"stop: {stop_old}→{stop_new}, TP1: {tp1_old}→{tp1_new}"
                        )
                        self.alerts.send_notify('exit_update', {
                            'ticker': ticker, 'trade_id': _tid,
                            'stop_old': stop_old, 'stop_new': stop_new,
                            'tp1_old': tp1_old,  'tp1_new': tp1_new,
                            'tp2_new': tp2_new,
                            'thesis': decision.get('thesis', ''),
                        })
                    else:
                        logger.warning(f"  🚫 {ticker} SKIPPED — already have open position (no updated levels to apply)")
                    # Revert conditional to active so it can re-trigger on next price check
                    _conn.execute(
                        "UPDATE conditional_tracking SET status='active', updated_at=? WHERE id=?",
                        (datetime.now().isoformat(), decision['conditional_id'])
                    )
                    _conn.commit(); _conn.close()
                except Exception as _e:
                    logger.warning(f"  ⚠️  Exit level update failed for {ticker}: {_e}")
                continue

            if not self.auto_execute:
                logger.info(f"  ℹ️  CONDITIONAL READY (auto_execute off): {ticker} @ ${entry_price:.2f} — ${position_size:,.0f}")
                self.decision_engine.record_decision(decision, executed=False, result="PENDING_AUTO")
                continue

            # SEMI_AUTO: Notify via Slack but do NOT execute — user must /buy
            if self.broker_mode == 'semi_auto':
                logger.info(f"  📢 CONDITIONAL TRIGGERED (semi_auto): {ticker} @ ${entry_price:.2f} — ${position_size:,.0f} — awaiting /buy")
                self._notify_acquisition_triggered(decision, executed=False)
                self.decision_engine.record_decision(decision, executed=False, result="PENDING_APPROVAL")

                # Register pending alert with orchestrator instance so /yes and pending drains work
                try:
                    # Best-effort: append to in-memory orchestrator pending queue if available
                    from hightrade_orchestrator import ORCH_INSTANCE
                    if ORCH_INSTANCE is not None:
                        pending_obj = {
                            'ticker': ticker,
                            'conditional_id': decision.get('conditional_id'),
                            'position_size': position_size,
                            'entry_price': entry_price,
                            'thesis': decision.get('thesis', ''),
                            'timestamp': datetime.now().isoformat(),
                            'executed': False,
                        }
                        ORCH_INSTANCE.pending_trade_alerts.append(pending_obj)
                        logger.info(f"  ➕ Pending trade alert registered in orchestrator: {ticker} (conditional_id={decision.get('conditional_id')})")
                except Exception as _e:
                    logger.warning(f"  ⚠️  Could not register pending alert with orchestrator: {_e}")

                # Also write a durable pending alert file so the orchestrator can pick it up
                try:
                    import json
                    from pathlib import Path
                    pending_file = Path(__file__).parent / 'trading_data' / 'pending_alerts.json'
                    pending_file.parent.mkdir(parents=True, exist_ok=True)
                    alerts = []
                    if pending_file.exists():
                        try:
                            with open(pending_file, 'r') as pf:
                                alerts = json.load(pf)
                        except Exception:
                            alerts = []
                    alerts.append({
                        'ticker': ticker,
                        'conditional_id': decision.get('conditional_id'),
                        'position_size': position_size,
                        'entry_price': entry_price,
                        'thesis': decision.get('thesis', ''),
                        'timestamp': datetime.now().isoformat(),
                        'executed': False,
                    })
                    with open(pending_file, 'w') as pf:
                        json.dump(alerts, pf, indent=2)
                    logger.info(f"  💾 Pending trade alert written to file: {pending_file}")
                except Exception as _e:
                    logger.warning(f"  ⚠️  Failed to write pending alert file: {_e}")

                executed_count += 1  # Count as "processed" for logging
                continue

            # FULL_AUTO: Execute immediately
            logger.info(f"  🤖 Executing acquisition entry: {ticker} @ ${entry_price:.2f} — ${position_size:,.0f}")

            # Execute as a single-name acquisition entry
            trade_alert = {
                'defcon_level':    3,  # Acquisition entries are pre-researched, lower urgency
                'signal_score':    decision['confidence'] * 100,
                'crisis_type':     'acquisition_conditional',
                'crisis_description': decision.get('thesis', f'Acquisition conditional for {ticker}'),
                'assets': {
                    'primary_asset':          ticker,
                    'secondary_asset':        None,
                    'tertiary_asset':         None,
                    'primary_allocation_pct': 1.0,
                    'secondary_allocation_pct': 0.0,
                    'tertiary_allocation_pct': 0.0,
                    'primary_size':           position_size,
                    'secondary_size':         0,
                    'tertiary_size':          0,
                },
                'total_position_size': position_size,
                'vix':             20.0,  # Conservative default — actual VIX not critical here
                'rationale':       decision.get('thesis', ''),
                'confidence_score': int(decision['confidence'] * 100),
                'risk_reward_analysis': (
                    f"Entry: ${entry_price:.2f} | "
                    f"Stop: ${decision.get('stop_loss', 0):.2f} | "
                    f"TP1: ${decision.get('take_profit_1', 0):.2f}"
                ),
                'time_window_minutes': 30,
            }

            try:
                shares = max(1, int(position_size / entry_price)) if entry_price > 0 else 0
                buy_result = self.decision_engine.paper_trading.manual_buy(
                    ticker,
                    shares,
                    price_override=entry_price,
                    notes=(
                        f"Acquisition conditional entry | thesis={decision.get('thesis', '')} | "
                        f"stop={decision.get('stop_loss', 0):.2f} | tp1={decision.get('take_profit_1', 0):.2f}"
                    ),
                )
                if buy_result.get('ok'):
                    executed_count += 1
                    open_tickers.add(ticker)  # Track so next conditional for same ticker is blocked
                    self.decision_engine.record_decision(decision, executed=True, result="ACQUISITION_ENTERED")
                    self._notify_acquisition_triggered(decision, executed=True)
                    trade_ids = [buy_result.get('trade_id')]
                    logger.info(f"  ✅ {ticker} acquisition entry executed (trade_id={buy_result.get('trade_id')})")
                    # Write analyst-derived exit levels to the new trade record
                    stop = decision.get('stop_loss')
                    tp1  = decision.get('take_profit_1')
                    tp2  = decision.get('take_profit_2')
                    if stop or tp1:
                        try:
                            _db = Path(__file__).parent / 'trading_data' / 'trading_history.db'
                            _conn = sqlite3.connect(str(_db))
                            for tid in trade_ids:
                                _conn.execute(
                                    "UPDATE trade_records SET stop_loss=?, take_profit_1=?, take_profit_2=? WHERE trade_id=?",
                                    (stop, tp1, tp2, tid)
                                )
                            _conn.commit(); _conn.close()
                            logger.info(f"  📌 {ticker} exit levels stored — stop=${stop}, TP1=${tp1}, TP2=${tp2}")
                        except Exception as _e:
                            logger.warning(f"  ⚠️  Could not write exit levels for {ticker}: {_e}")
                else:
                    logger.warning(f"  ❌ {ticker} acquisition entry failed: {buy_result.get('message', 'unknown error')}")
                    self.decision_engine.record_decision(decision, executed=False, result="EXECUTION_FAILED")
            except Exception as e:
                logger.error(f"  ❌ {ticker} acquisition entry failed: {e}")
                self.decision_engine.record_decision(decision, executed=False, result="EXECUTION_FAILED")

        return executed_count

    def _notify_acquisition_triggered(self, decision: Dict, executed: bool = True):
        """Send Slack notification for an acquisition conditional (triggered or executed).

        Uses send_slack (→ #hightrade) directly instead of send_defcon_alert,
        which is gated on DEFCON thresholds and silently drops acquisition alerts.
        """
        try:
            if not self._mark_acquisition_alert_sent(decision, executed):
                logger.info(
                    f"  🔕 Duplicate acquisition Slack alert suppressed: "
                    f"{decision.get('ticker', '?')} (conditional_id={decision.get('conditional_id')}, executed={executed})"
                )
                return

            ticker     = decision['ticker']
            price      = decision['current_price']
            size       = decision['position_size']
            confidence = decision['confidence']
            stop       = decision.get('stop_loss', 0)
            tp1        = decision.get('take_profit_1', 0)
            tp2        = decision.get('take_profit_2', 0)
            thesis     = decision.get('thesis', '')
            conditions = decision.get('entry_conditions', [])
            cond_text  = '\n'.join(f"  • {c}" for c in conditions[:3]) if conditions else '  • N/A'

            watch_tag  = decision.get('watch_tag', '')
            tag_label  = f" `[{watch_tag}]`" if watch_tag else ""

            if executed:
                header = f"🎯 *ACQUISITION ENTRY EXECUTED*{tag_label}"
            else:
                header = f"📢 *ACQUISITION CONDITIONAL TRIGGERED*{tag_label} — awaiting `/buy`"

            message = (
                f"{header}\n"
                f"{'─'*40}\n"
                f"Ticker: *{ticker}* @ ${price:.2f}\n"
                f"Position: ${size:,.0f} ({decision.get('position_size_pct',0)*100:.0f}% of cash)\n"
                f"Confidence: {confidence:.2f}\n\n"
                f"📐 Levels:\n"
                f"  Stop loss: ${stop:.2f}\n"
                f"  Take profit 1: ${tp1:.2f}\n"
                f"  Take profit 2: ${tp2:.2f}\n\n"
                f"📋 Entry conditions met:\n{cond_text}\n\n"
                f"💡 Thesis: {thesis}"
            )
            # Semi-auto (awaiting /buy) → #hightrade so user sees it
            # Full-auto (executed)     → #logs-silent (confirmation noise)
            self.notification_engine.alerts.send_acquisition_alert(
                message, primary=not executed
            )
        except Exception as e:
            logger.warning(f"Acquisition notification failed: {e}")

    def _send_market_tips(self, defcon_level: int, signal_score: float, decision: Dict):
        """Send helpful trading tips based on market conditions"""
        tips = []

        if signal_score > 80:
            tips.append("💡 Strong signal detected - this is a high-confidence setup")

        if defcon_level == 1:
            tips.append("🚨 DEFCON 1 reached - maximum market stress, positions sized down")

        performance = self._get_performance_tips()
        if performance:
            tips.append(performance)

        for tip in tips:
            logger.info(tip)

    def _get_performance_tips(self) -> Optional[str]:
        """Get performance-based tips"""
        perf = self.decision_engine.paper_trading.get_portfolio_performance()

        if perf['closed_trades'] > 5:
            if perf['win_rate'] > 60:
                return "📈 Excellent win rate (>60%) - system is performing well"
            elif perf['win_rate'] < 40:
                return "📉 Low win rate (<40%) - consider adjusting strategy"

        return None

    def get_status(self) -> Dict:
        """Get current broker status"""
        return {
            'auto_execute': self.auto_execute,
            'broker_mode': self.broker_mode,
            'trades_today': self.trades_executed_today,
            'daily_limit': self.max_daily_trades,
            'can_trade': self.trades_executed_today < self.max_daily_trades,
            'decision_history_size': len(self.decision_engine.decision_history)
        }


def main():
    """Test broker system"""
    import logging as log
    log.basicConfig(level=log.INFO, format='%(levelname)s: %(message)s')

    print("\n" + "="*70)
    print("AUTONOMOUS BROKER AGENT - TEST")
    print("="*70)

    # Initialize broker
    broker = AutonomousBroker(auto_execute=False)  # Start with auto_execute=False for testing

    # Test buy decision
    print("\n📊 Testing Buy Decision...")
    test_market_data = {'vix': 25.0}
    buy_result = broker.process_market_conditions(
        defcon_level=2,
        signal_score=75.0,
        crisis_description="Tariff announcement and supply chain concerns",
        market_data=test_market_data
    )
    print(f"Buy Decision Result: {buy_result}")

    # Test exit detection
    print("\n🔍 Testing Exit Detection...")
    exits = broker.process_exits()
    print(f"Exits Detected: {exits}")

    # Get status
    status = broker.get_status()
    print(f"\nBroker Status: {status}")

    print("\n" + "="*70 + "\n")


if __name__ == '__main__':
    main()


def _enforce_safety_before_mirror(self, ticker, shares, notional):
    # Called before mirroring DB trade to broker
    if is_e_stop_active():
        raise RuntimeError('E-STOP active: aborting mirror')
    per_order = get_limit('per_order_max', None)
    if per_order is not None and notional > per_order:
        raise RuntimeError(f'Per-order notional {notional} exceeds per_order_max {per_order}')

