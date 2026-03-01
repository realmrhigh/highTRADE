#!/usr/bin/env python3
"""
HighTrade Broker Agent - Autonomous Trading Decision System
Analyzes market conditions, makes trade decisions, and executes on your behalf
"""

import sqlite3
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_ET = ZoneInfo('America/New_York')
def _et_now() -> datetime:
    return datetime.now(_ET)
from paper_trading import PaperTradingEngine, CrisisAssetIntelligence
from alerts import AlertSystem
from quick_money_research import QuickMoneyResearch

# Use SCRIPT_DIR to ensure we're in the correct project directory
SCRIPT_DIR = Path(__file__).parent.resolve()
DB_PATH = SCRIPT_DIR / 'trading_data' / 'trading_history.db'

logger = logging.getLogger(__name__)


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
    """
    ticker      = exit.get('asset_symbol', '')
    exit_price  = exit.get('current_price', 0)
    entry_price = exit.get('entry_price', 0)
    loss_pct    = exit.get('profit_loss_pct', 0) * 100      # e.g. -3.2
    loss_dollars = exit.get('profit_loss_dollars', 0)
    date_str    = datetime.now().strftime('%Y-%m-%d')

    if not ticker:
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
        conn = sqlite3.connect(str(DB_PATH))
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


class BrokerDecisionEngine:
    """Makes autonomous trading decisions"""

    def __init__(self):
        self.paper_trading = PaperTradingEngine()
        self.intelligence = CrisisAssetIntelligence()
        self.alerts = AlertSystem()
        self.quick_money = QuickMoneyResearch()
        self.decision_history = []

    def analyze_market_for_trades(self, defcon_level: int, signal_score: float,
                                  crisis_description: str, market_data: Dict) -> Optional[Dict]:
        """
        Analyze market conditions and decide whether to execute a trade

        Returns trade decision or None if no trade warranted
        """
        vix = market_data.get('vix', 20.0)

        # Decision 1: Should we trade at all?
        if not self._should_trade(defcon_level, signal_score):
            logger.info("❌ Trade criteria not met - skipping")
            return None

        # Decision 2: What assets should we trade?
        crisis_type = self.intelligence.analyze_crisis_type(crisis_description, signal_score)
        recommendations = self.intelligence.recommend_assets_for_crisis(
            crisis_type, signal_score, defcon_level
        )

        # Decision 3: How much should we trade?
        position_size = self.paper_trading.calculate_position_size_vix_adjusted(vix)

        # Decision 4: Risk check - don't over-expose
        # Use available cash to account for realized P&L
        current_exposure = self._calculate_current_exposure()
        available_cash = self._calculate_available_cash()
        effective_capital = current_exposure + available_cash  # true account value minus unrealized
        if current_exposure + position_size > effective_capital * 0.60:
            logger.warning(f"⚠️  Portfolio exposure limit reached ({current_exposure + position_size:.0f})")
            return None

        # Build trade decision
        decision = {
            'timestamp': datetime.now().isoformat(),
            'decision_type': 'BUY_PACKAGE',
            'confidence': recommendations['confidence_score'],
            'crisis_type': crisis_type,
            'assets': {
                'primary': recommendations['primary_asset'],
                'secondary': recommendations['secondary_asset'],
                'tertiary': recommendations['tertiary_asset']
            },
            'position_size': position_size,
            'vix': vix,
            'defcon_level': defcon_level,
            'signal_score': signal_score,
            'rationale': recommendations['rationale']
        }

        logger.info(f"✅ BUY DECISION: {crisis_type} - Size: ${position_size:,.0f}, Confidence: {decision['confidence']}%")
        return decision

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
                   defcon_at_entry, shares, entry_date, stop_loss, take_profit_1
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
            tp1_price  = trade.get('take_profit_1')
            stop_price = trade.get('stop_loss')
            tp_threshold = ((tp1_price - entry_price) / entry_price) if tp1_price else self.paper_trading.PROFIT_TARGET
            sl_threshold = ((stop_price - entry_price) / entry_price) if stop_price else self.paper_trading.STOP_LOSS
            tp_src  = f"${tp1_price:.2f}" if tp1_price else f"{self.paper_trading.PROFIT_TARGET*100:.0f}% (default)"
            sl_src  = f"${stop_price:.2f}" if stop_price else f"{abs(self.paper_trading.STOP_LOSS*100):.0f}% (default)"

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

            # Decision 2: Hit stop loss?
            elif profit_loss_pct <= sl_threshold:
                decision = {
                    'trade_id': trade['trade_id'],
                    'asset_symbol': trade['asset_symbol'],
                    'decision_type': 'SELL_STOP_LOSS',
                    'entry_price': entry_price,
                    'current_price': current_price,
                    'profit_loss_pct': profit_loss_pct,
                    'profit_loss_dollars': profit_loss_dollars,
                    'reason': f"Stop loss triggered ({sl_src}): {profit_loss_pct*100:.2f}%",
                    'confidence': 100
                }
                exit_decisions.append(decision)
                logger.warning(f"🛑 EXIT: {trade['asset_symbol']} - Stop loss ({sl_src})! {profit_loss_pct*100:.2f}%")

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
        # Don't trade during DEFCON 5 (peaceful times)
        if defcon_level > 2:
            return False

        # Don't trade if signal score too low
        if signal_score < 60:
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
        conn = sqlite3.connect(str(DB_PATH))
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
        conn = sqlite3.connect(str(DB_PATH))
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
        Run a Gemini 3 Pro check immediately before triggering an acquisition.
        Returns {"approve": bool, "reason": str, "veto_reason": str, "conditions_met": list}.
        On any error, defaults to APPROVE (fail-open) so a Gemini outage doesn't block all trading.
        """
        import gemini_client

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

        prompt = (
            f"You are a pre-purchase risk gate for an automated paper trading system.\n"
            f"A conditional entry just triggered for {ticker} (watch_tag: {tag}).\n\n"
            f"{_session_ctx}\n"
            f"ORIGINAL THESIS:\n{thesis}\n\n"
            f"TRADE LEVELS:\n"
            f"  Entry target: ${entry_tgt} | Current price: ${current_price:.2f}\n"
            f"  Stop loss: ${stop} | Take profit 1: ${tp1}\n\n"
            f"ANALYST'S ENTRY CONDITIONS (must ALL be true to enter):\n"
            f"{entry_conds_text}\n\n"
            f"INVALIDATION CONDITIONS (if any triggered, do NOT enter):\n"
            f"{inval_conds_text}\n\n"
            f"CURRENT LIVE STATE (captured at trigger time):\n"
            f"  VIX: {vix}\n"
            f"  DEFCON: {defcon}/5\n"
            f"  News score: {news_score}/100\n"
            f"  Macro composite score: {macro_score}/100\n\n"
            f"YOUR JOB:\n"
            f"1. Check each entry condition against the live state. Are they met?\n"
            f"2. Check each invalidation condition. Has any been triggered?\n"
            f"3. Given the watch_tag '{tag}', does this entry make sense right now?\n"
            f"4. Approve or veto this purchase.\n\n"
            f"Respond ONLY in this exact JSON (no other text):\n"
            f'{{\n'
            f'  "approve": true,\n'
            f'  "conditions_met": ["condition 1: PASS/FAIL — reason", "condition 2: PASS/FAIL — reason"],\n'
            f'  "reason": "brief reason for approval (empty if vetoing)",\n'
            f'  "veto_reason": "detailed reason for veto (empty if approving)",\n'
            f'  "data_gaps": ["<data absent at trigger time that would have made this decision sharper — e.g. \'real-time options flow\', \'volume confirmation not yet available\', \'earnings in 3 days not flagged in entry conditions\'>"] \n'
            f'}}'
        )

        try:
            text, in_tok, out_tok = gemini_client.call(prompt=prompt, model_key='balanced')
            logger.info(f"  🔍 Pre-purchase gate [{ticker}]: {in_tok}→{out_tok} tok")

            # Parse JSON
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()
            if "<think>" in text:
                parts = text.split("</think>")
                if len(parts) > 1:
                    text = parts[-1].strip()

            result = json.loads(text.strip())
            return result

        except json.JSONDecodeError as e:
            logger.warning(f"  ⚠️  Gate JSON parse failed for {ticker}: {e} — defaulting to APPROVE")
            return {"approve": True, "reason": "gate parse error — fail-open", "veto_reason": "", "conditions_met": []}
        except Exception as e:
            logger.warning(f"  ⚠️  Pre-purchase gate failed for {ticker}: {e} — defaulting to APPROVE")
            return {"approve": True, "reason": "gate error — fail-open", "veto_reason": "", "conditions_met": []}

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
        import sqlite3
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
            cursor = conn.execute("""
                SELECT id, ticker, date_created, entry_price_target,
                       stop_loss, take_profit_1, take_profit_2,
                       position_size_pct, time_horizon_days,
                       thesis_summary, research_confidence,
                       entry_conditions_json, invalidation_conditions_json,
                       watch_tag, watch_tag_rationale
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

            logger.debug(f"  📊 {ticker}: current=${current_price:.2f}, target=${entry_target:.2f}")

            # Trigger check: price has reached or dropped to entry target
            if current_price <= entry_target and ticker not in triggered_tickers:
                # Calculate position size using actual available cash (accounts for realized P&L)
                available_cash = self._calculate_available_cash()
                raw_pct        = float(cond.get('position_size_pct') or 0.05)
                confidence     = float(cond.get('research_confidence') or 0.5)
                # Formulaic sizing: cash * confidence * analyst_size_pct, capped at 20%
                MAX_PCT = 0.20
                effective_pct  = min(raw_pct * confidence, MAX_PCT)
                position_dollars = available_cash * effective_pct

                if position_dollars < 100:
                    logger.warning(f"  ⚠️  {ticker} position too small (${position_dollars:.0f}) — skipping")
                    continue

                # Exposure guard
                if self._calculate_current_exposure() + position_dollars > self.paper_trading.total_capital * 0.60:
                    logger.warning(f"  ⚠️  {ticker} would breach 60% exposure cap — skipping")
                    continue

                # ── PRE-PURCHASE GATE: Gemini 3 Pro live conditions check ──────
                watch_tag = cond.get('watch_tag') or 'untagged'
                logger.info(
                    f"  🎯 {ticker} [{watch_tag}] price triggered: "
                    f"${current_price:.2f} <= ${entry_target:.2f} — running Pro gate..."
                )
                gate = self._run_pre_purchase_gate(cond, current_price, live_state)

                if not gate.get('approve', True):
                    veto = gate.get('veto_reason', 'unspecified')
                    logger.warning(f"  🚫 {ticker} VETOED by pre-purchase gate: {veto}")
                    # Leave as active — will retry next cycle when conditions change
                    continue

                logger.info(
                    f"  ✅ {ticker} gate APPROVED: {gate.get('reason', 'conditions met')}"
                )
                gate_gaps = gate.get('data_gaps', [])
                if gate_gaps:
                    logger.info(f"  🔍 Gate data gaps ({ticker}): {' | '.join(gate_gaps)}")

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
        """Notify user about a sell decision"""
        profit_loss_color = "📈" if decision['profit_loss_dollars'] > 0 else "📉"

        message = f"""
💼 BROKER ACTION: SELL EXECUTED
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Decision: Autonomous Sell Executed
Time: {datetime.now().isoformat()}
Reason: {decision['reason']}

Asset: {decision['asset_symbol']}
Trade ID: {decision['trade_id']}

Entry Price: ${decision['entry_price']:.2f}
Exit Price: ${decision['current_price']:.2f}

Result:
  {profit_loss_color} Profit/Loss: ${decision['profit_loss_dollars']:+,.0f}
  {profit_loss_color} Return: {decision['profit_loss_pct']:+.2f}%

Exit Type: {decision['decision_type']}

Your broker closed this position on your behalf.
Check portfolio: python3 trading_cli.py status
"""
        # Send via all enabled channels (Slack, email, etc)
        self.alerts.send_defcon_alert(
            defcon_level=1,
            signal_score=decision.get('confidence', 100),
            details=message
        )
        logger.info("📨 Sell notification sent to all channels")

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

            # Build alert for execution
            alert = {
                'defcon_level': trade_decision['defcon_level'],
                'signal_score': trade_decision['signal_score'],
                'crisis_type': trade_decision['crisis_type'],
                'assets': {
                    'primary_asset': trade_decision['assets']['primary'],
                    'secondary_asset': trade_decision['assets']['secondary'],
                    'tertiary_asset': trade_decision['assets']['tertiary'],
                    'primary_allocation_pct': 0.50,
                    'secondary_allocation_pct': 0.30,
                    'tertiary_allocation_pct': 0.20,
                    'primary_size': trade_decision['position_size'] * 0.50,
                    'secondary_size': trade_decision['position_size'] * 0.30,
                    'tertiary_size': trade_decision['position_size'] * 0.20
                },
                'total_position_size': trade_decision['position_size'],
                'vix': trade_decision['vix'],
                'rationale': trade_decision['rationale'],
                'confidence_score': trade_decision['confidence'],
                'crisis_description': trade_decision.get('rationale', 'Autonomous broker decision'),
                'risk_reward_analysis': '',
                'time_window_minutes': 15
            }

            # Execute the trade
            trade_ids = self.decision_engine.paper_trading.execute_trade_package(alert, user_approval=True)

            if trade_ids:
                self.trades_executed_today += 1
                self.notification_engine.send_buy_notification(trade_decision)
                self.decision_engine.record_decision(trade_decision, executed=True, result="EXECUTED")

                # Send tips
                self._send_market_tips(defcon_level, signal_score, trade_decision)

                return True
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
                    'SELL_PROFIT_TARGET': 'profit_target',
                    'SELL_STOP_LOSS': 'stop_loss',
                    'SELL_EARLY_PROFIT': 'profit_target',
                    'SELL_MANUAL': 'manual',
                    'SELL_TRAILING_STOP': 'stop_loss',
                    'SELL_TIME_LIMIT': 'manual',
                    'SELL_DEFCON_REVERT': 'manual',
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
                executed_count += 1  # Count as "processed" for logging
                continue

            # FULL_AUTO: Execute immediately
            logger.info(f"  🤖 Executing acquisition entry: {ticker} @ ${entry_price:.2f} — ${position_size:,.0f}")

            # Build a trade package compatible with PaperTradingEngine.execute_trade_package
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
                trade_ids = self.decision_engine.paper_trading.execute_trade_package(
                    trade_alert, user_approval=True
                )
                if trade_ids:
                    executed_count += 1
                    open_tickers.add(ticker)  # Track so next conditional for same ticker is blocked
                    self.decision_engine.record_decision(decision, executed=True, result="ACQUISITION_ENTERED")
                    self._notify_acquisition_triggered(decision, executed=True)
                    logger.info(f"  ✅ {ticker} acquisition entry executed (trade_ids={trade_ids})")
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
                    logger.warning(f"  ⚠️  {ticker} entry returned no trade IDs")
            except Exception as e:
                logger.error(f"  ❌ {ticker} acquisition entry failed: {e}")

        return executed_count

    def _notify_acquisition_triggered(self, decision: Dict, executed: bool = True):
        """Send Slack notification for an acquisition conditional (triggered or executed).

        Uses send_slack (→ #hightrade) directly instead of send_defcon_alert,
        which is gated on DEFCON thresholds and silently drops acquisition alerts.
        """
        try:
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
            # Route to #logs-silent — acquisition pipeline noise, not a trade signal
            self.notification_engine.alerts.send_acquisition_alert(message)
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
