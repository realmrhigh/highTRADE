#!/usr/bin/env python3
"""
alpaca_stream.py — Real-time Alpaca WebSocket price stream for HighTrade.

Replaces yfinance polling with sub-second price updates for:
  1. ENTRY TRIGGERS — active conditionals in conditional_tracking
  2. EXIT MONITORING — open positions (trailing stop, thesis floor, TP)
  3. PEAK TRACKING — real-time high-watermark for trailing stop accuracy

Runs in a background thread, managed by the orchestrator.

Architecture:
  • One StockDataStream WebSocket per process (Alpaca limit)
  • Subscriptions auto-refresh every REFRESH_INTERVAL_SEC or on-demand
  • Trade ticks update an in-memory price cache; heavy logic (Pro gate,
    Alpaca order submission) is dispatched to the main thread via a queue
  • IEX feed (free tier) — upgrade to SIP in .env if you want full market

Usage from orchestrator:
    from alpaca_stream import RealtimeMonitor
    monitor = RealtimeMonitor(broker=self.broker, paper_trading=self.paper_trading)
    monitor.start()         # background thread
    monitor.stop()          # graceful shutdown
    monitor.get_status()    # dict for dashboard
"""

import asyncio
import json
import logging
import importlib.util
import os
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_ET = ZoneInfo('America/New_York')
SCRIPT_DIR = Path(__file__).parent.resolve()
DB_PATH = SCRIPT_DIR / 'trading_data' / 'trading_history.db'

# ── Configuration ─────────────────────────────────────────────────────────────

TRAILING_STOP_PCT = 0.03          # 3% trailing from peak — must match broker_agent.py
REFRESH_INTERVAL_SEC = 60         # Re-scan DB for new conditionals / positions every 60s
DEBOUNCE_TRIGGER_SEC = 5          # Don't re-trigger same ticker within 5s
STREAM_HEALTH_LOG_SEC = 300       # Log stream health every 5 min
RECONNECT_DELAY_SEC = 5           # Wait before reconnecting after disconnect
MAX_RECONNECT_ATTEMPTS = 50       # Give up after this many consecutive failures
PROXIMITY_ALERT_PCT = 0.02        # Alert when price within 2% of target

# Breakout entry constants — duplicated from broker_agent.py to avoid heavy import chain
# (broker_agent pulls in paper_trading, alerts, gemini_client, etc.)
UPSIDE_TRIGGER_TAGS = {'breakout'}
BREAKOUT_MAX_EXTENSION = 0.10

IEX_SYMBOL_LIMIT = 30             # Free IEX tier max symbols (upgrade to SIP for unlimited)
SIP_SYMBOL_LIMIT = 500            # SIP tier practical limit
ROTATION_INTERVAL_SEC = 120       # Rotate overflow tickers every 2 minutes


class RealtimeMonitor:
    """
    Real-time WebSocket price monitor for entry triggers and exit management.

    Subscribes to Alpaca's StockDataStream for all watched tickers and fires
    callbacks when price thresholds are crossed.
    """

    def __init__(self, broker=None, paper_trading=None):
        """
        broker:        AutonomousBroker instance (for process_acquisition_conditionals)
        paper_trading: PaperTradingEngine instance (for position data)
        """
        self.broker = broker
        self.paper_trading = paper_trading

        # Alpaca credentials
        self.api_key = os.getenv('ALPACA_API_KEY', '')
        self.secret_key = os.getenv('ALPACA_SECRET_KEY', '')
        self.base_url = os.getenv('ALPACA_BASE_URL', 'https://paper-api.alpaca.markets')
        self._configured = bool(self.api_key and self.secret_key)
        self._feed = os.getenv('ALPACA_DATA_FEED', 'iex').lower()  # 'iex' (free) or 'sip'

        # State
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._stream = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Subscribed tickers and their targets
        self._subscribed_tickers: Set[str] = set()
        self._conditionals: Dict[str, dict] = {}       # ticker → {id, entry_target, stop, tp1, ...}
        self._positions: Dict[str, dict] = {}           # ticker → {trade_id, entry, peak, stop, tp1}
        self._last_prices: Dict[str, float] = {}        # ticker → latest price
        self._last_trigger_time: Dict[str, float] = {}  # ticker → time.time() of last trigger
        self._dispatch_locks: Dict[str, threading.Lock] = defaultdict(threading.Lock)  # per-ticker gate lock

        # Stats for dashboard
        self._stats = {
            'status': 'initializing',
            'started_at': None,
            'last_tick_at': None,
            'last_trigger_at': None,
            'last_trigger_ticker': None,
            'last_trigger_type': None,
            'ticks_received': 0,
            'ticks_per_sec': 0.0,
            'subscribed_tickers': 0,
            'entry_triggers': 0,
            'exit_triggers': 0,
            'peak_updates': 0,
            'proximity_alerts': 0,
            'reconnects': 0,
            'errors': 0,
            'last_error': None,
            'feed': 'unknown',
            'overflow_count': 0,
        }
        self._tick_window: List[float] = []   # timestamps for TPS calculation

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self):
        """Start the real-time monitor in a background thread."""
        if not self._sdk_available():
            logger.warning(
                "⚠️  RealtimeMonitor: Alpaca SDK not installed (`alpaca-py`) — stream disabled"
            )
            self._stats['status'] = 'disabled (alpaca-py missing)'
            self._stats['last_error'] = 'alpaca-py package not installed'
            return

        if not self._configured:
            logger.warning("⚠️  RealtimeMonitor: ALPACA_API_KEY / ALPACA_SECRET_KEY not set — stream disabled")
            self._stats['status'] = 'disabled (no API keys)'
            self._stats['last_error'] = 'missing ALPACA_API_KEY / ALPACA_SECRET_KEY'
            return

        if self._thread and self._thread.is_alive():
            logger.warning("RealtimeMonitor already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_thread, daemon=True, name='alpaca-stream')
        self._thread.start()
        logger.info("🔴 RealtimeMonitor started (background thread)")

    def stop(self):
        """Gracefully shut down the stream."""
        self._stop_event.set()
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=10)
        self._stats['status'] = 'stopped'
        logger.info("🔴 RealtimeMonitor stopped")

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def get_status(self) -> dict:
        """Return stream health dict for the dashboard."""
        s = dict(self._stats)
        s['subscribed_tickers'] = len(self._subscribed_tickers)
        s['subscribed_list'] = sorted(self._subscribed_tickers)
        s['conditionals_watched'] = len(self._conditionals)
        s['positions_watched'] = len(self._positions)
        s['total_tickers_needed'] = len(set(self._conditionals.keys()) | set(self._positions.keys()))
        s['symbol_limit'] = SIP_SYMBOL_LIMIT if self._feed == 'sip' else IEX_SYMBOL_LIMIT

        # Calculate ticks/sec over last 60s
        now = time.time()
        self._tick_window = [t for t in self._tick_window if now - t < 60]
        s['ticks_per_sec'] = round(len(self._tick_window) / 60.0, 1) if self._tick_window else 0

        return s

    def get_price(self, ticker: str) -> Optional[float]:
        """Return the latest streamed price for a ticker, or None."""
        return self._last_prices.get(ticker)

    def force_refresh(self):
        """Force a subscription refresh on next cycle (e.g. after new conditional added)."""
        self._refresh_subscriptions()

    def _sdk_available(self) -> bool:
        """Return True when the Alpaca SDK is importable in the active runtime."""
        return importlib.util.find_spec('alpaca') is not None

    # ── Background Thread ─────────────────────────────────────────────────────

    def _run_thread(self):
        """Main thread entry point — runs the asyncio event loop."""
        self._stats['started_at'] = datetime.now().isoformat()
        self._stats['feed'] = self._feed
        reconnect_attempts = 0

        while not self._stop_event.is_set() and reconnect_attempts < MAX_RECONNECT_ATTEMPTS:
            try:
                self._stats['status'] = 'connecting'
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)
                self._loop.run_until_complete(self._stream_loop())
            except Exception as e:
                reconnect_attempts += 1
                self._stats['reconnects'] += 1
                self._stats['errors'] += 1
                self._stats['last_error'] = f"{e} (attempt {reconnect_attempts})"
                self._stats['status'] = f'reconnecting ({reconnect_attempts})'
                logger.warning(
                    f"🔴 Stream disconnected: {e} — reconnecting in {RECONNECT_DELAY_SEC}s "
                    f"(attempt {reconnect_attempts}/{MAX_RECONNECT_ATTEMPTS})"
                )
                if not self._stop_event.is_set():
                    time.sleep(RECONNECT_DELAY_SEC)
            else:
                # Clean exit (stop_event was set)
                break

        if reconnect_attempts >= MAX_RECONNECT_ATTEMPTS:
            self._stats['status'] = 'failed (max reconnects)'
            logger.error(f"🔴 RealtimeMonitor gave up after {MAX_RECONNECT_ATTEMPTS} reconnect attempts")

    async def _stream_loop(self):
        """Async loop — connect, subscribe, and process ticks."""
        from alpaca.data.live import StockDataStream
        from alpaca.data.enums import DataFeed

        feed = DataFeed.SIP if self._feed == 'sip' else DataFeed.IEX

        self._stream = StockDataStream(
            api_key=self.api_key,
            secret_key=self.secret_key,
            feed=feed,
        )

        # Load initial subscriptions
        self._refresh_subscriptions()

        if not self._subscribed_tickers:
            logger.info("🔴 No tickers to monitor — waiting for conditionals or positions")
            self._stats['status'] = 'idle (no tickers)'
            # Poll for new tickers every 30s
            while not self._stop_event.is_set():
                await asyncio.sleep(30)
                self._refresh_subscriptions()
                if self._subscribed_tickers:
                    break
            if self._stop_event.is_set():
                return

        # Subscribe to trades for all tickers
        tickers_list = sorted(self._subscribed_tickers)
        logger.info(f"🔴 Subscribing to {len(tickers_list)} tickers: {', '.join(tickers_list[:15])}{'...' if len(tickers_list) > 15 else ''}")

        self._stream.subscribe_trades(self._on_trade, *tickers_list)
        self._stats['status'] = 'streaming'

        # Start a refresh task that periodically re-scans the DB
        refresh_task = asyncio.ensure_future(self._periodic_refresh())

        # Start a health logging task
        health_task = asyncio.ensure_future(self._periodic_health_log())

        try:
            # The stream.run() blocks until disconnected
            await asyncio.to_thread(self._run_stream_blocking)
        finally:
            refresh_task.cancel()
            health_task.cancel()

    def _run_stream_blocking(self):
        """Run the stream in a way that can be interrupted."""
        try:
            self._stream.run()
        except Exception as e:
            if not self._stop_event.is_set():
                raise

    async def _periodic_refresh(self):
        """Periodically refresh subscriptions from DB."""
        while not self._stop_event.is_set():
            await asyncio.sleep(REFRESH_INTERVAL_SEC)
            try:
                old_tickers = set(self._subscribed_tickers)
                self._refresh_subscriptions()
                new_tickers = self._subscribed_tickers - old_tickers
                removed_tickers = old_tickers - self._subscribed_tickers

                # Remove first, then add — prevents hitting the symbol limit (405)
                # when swapping tickers at capacity.
                if removed_tickers:
                    logger.info(f"🔴 Stream: removing {len(removed_tickers)} tickers: {', '.join(sorted(removed_tickers))}")
                    self._stream.unsubscribe_trades(*removed_tickers)

                if new_tickers:
                    logger.info(f"🔴 Stream: adding {len(new_tickers)} tickers: {', '.join(sorted(new_tickers))}")
                    self._stream.subscribe_trades(self._on_trade, *new_tickers)

            except Exception as e:
                logger.warning(f"🔴 Subscription refresh failed: {e}")

    async def _periodic_health_log(self):
        """Log stream health stats periodically."""
        while not self._stop_event.is_set():
            await asyncio.sleep(STREAM_HEALTH_LOG_SEC)
            status = self.get_status()
            logger.info(
                f"🔴 Stream health: {status['ticks_received']} ticks | "
                f"{status['ticks_per_sec']}/s | {status['subscribed_tickers']} tickers | "
                f"{status['entry_triggers']} entries | {status['exit_triggers']} exits | "
                f"{status['peak_updates']} peak updates"
            )
            # Persist to DB for dashboard
            self._write_health_to_db(status)

    # ── Subscription Management ───────────────────────────────────────────────

    def _prioritize_tickers(self) -> List[str]:
        """
        Rank tickers by priority and return a list capped to the feed's symbol limit.

        Priority order:
          1. Open positions (MUST monitor for exits — trailing stop, thesis floor)
          2. Conditionals closest to entry target (highest urgency)
          3. Remaining conditionals (rotate through overflow pool)

        Returns the top N tickers that fit within the feed limit.
        """
        limit = SIP_SYMBOL_LIMIT if self._feed == 'sip' else IEX_SYMBOL_LIMIT
        all_needed = set(self._positions.keys()) | set(self._conditionals.keys())

        # If under limit, no prioritization needed
        if len(all_needed) <= limit:
            return sorted(all_needed)

        priority_list: List[str] = []

        # Tier 1: Open positions always get a slot
        position_tickers = sorted(self._positions.keys())
        priority_list.extend(position_tickers)

        remaining_slots = limit - len(priority_list)
        if remaining_slots <= 0:
            # More positions than slots?! Unlikely but handle it.
            logger.warning(f"🔴 {len(position_tickers)} open positions exceed {limit} symbol limit!")
            return priority_list[:limit]

        # Tier 2: Score conditionals by proximity to target.
        # Tickers with a known last_price closer to entry_target rank higher.
        cond_only = [t for t in self._conditionals if t not in self._positions]
        scored = []
        for t in cond_only:
            cond = self._conditionals[t]
            target = cond.get('entry_price_target')
            last = self._last_prices.get(t)
            if target and last and target > 0:
                # Distance from target as fraction (lower = closer = higher priority)
                dist = (last - target) / target
                scored.append((dist, t))
            else:
                # No price data yet — assign neutral distance so it gets rotated in
                scored.append((0.5, t))

        scored.sort(key=lambda x: x[0])  # closest first

        # Take the closest conditionals up to remaining slots
        closest = [t for _, t in scored[:remaining_slots]]
        overflow = [t for _, t in scored[remaining_slots:]]
        priority_list.extend(closest)

        # Log overflow for transparency
        if overflow:
            # Rotate: on each refresh cycle, shift the overflow pool so every
            # ticker gets some real-time coverage over time
            if not hasattr(self, '_rotation_offset'):
                self._rotation_offset = 0

            # Swap some overflow tickers in by rotating
            swap_count = min(len(overflow), max(1, remaining_slots // 3))  # rotate ~1/3 each cycle
            rot = self._rotation_offset % len(overflow) if overflow else 0

            # Pick 'swap_count' from overflow starting at rotation offset
            to_swap_in = []
            for i in range(swap_count):
                idx = (rot + i) % len(overflow)
                to_swap_in.append(overflow[idx])

            # Replace the LAST 'swap_count' items in closest with overflow rotations
            if to_swap_in and len(closest) > swap_count:
                for i, swap_ticker in enumerate(to_swap_in):
                    drop_idx = len(closest) - 1 - i
                    dropped = closest[drop_idx]
                    priority_list.remove(dropped)
                    priority_list.append(swap_ticker)

            self._rotation_offset += swap_count

            logger.info(
                f"🔴 Symbol limit: {len(all_needed)} tickers → capped to {limit}. "
                f"Monitoring {len(position_tickers)} positions + {remaining_slots} "
                f"closest conditionals. {len(overflow)} in rotation pool."
            )

        self._stats['overflow_count'] = len(overflow) if overflow else 0
        return sorted(set(priority_list[:limit]))

    def _refresh_subscriptions(self):
        """Scan DB for active conditionals + open positions and update subscription set."""
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=5)
            conn.row_factory = sqlite3.Row

            # 1. Active conditionals
            cond_rows = conn.execute("""
                SELECT id, ticker, entry_price_target, stop_loss, take_profit_1,
                       take_profit_2, position_size_pct, time_horizon_days,
                       thesis_summary, research_confidence, watch_tag,
                       entry_conditions_json, invalidation_conditions_json
                FROM conditional_tracking
                WHERE status = 'active'
            """).fetchall()

            self._conditionals = {}
            for r in cond_rows:
                row = dict(r)
                ticker = row['ticker']
                self._conditionals[ticker] = row

            # 2. Open positions
            pos_rows = conn.execute("""
                SELECT trade_id, asset_symbol, entry_price, peak_price,
                       stop_loss, take_profit_1, shares,
                       catalyst_event, catalyst_window_end,
                       catalyst_spike_pct, catalyst_failure_pct
                FROM trade_records
                WHERE status = 'open'
            """).fetchall()

            self._positions = {}
            for r in pos_rows:
                row = dict(r)
                ticker = row['asset_symbol']
                self._positions[ticker] = row

            conn.close()

            # Prioritize and cap to feed limit
            self._subscribed_tickers = set(self._prioritize_tickers())

        except Exception as e:
            logger.warning(f"🔴 Subscription refresh DB error: {e}")
            self._stats['errors'] += 1
            self._stats['last_error'] = str(e)

    # ── Trade Tick Handler ────────────────────────────────────────────────────

    async def _on_trade(self, trade):
        """
        Called on every trade tick from the WebSocket.
        This is the hot path — keep it FAST.
        Heavy logic (Pro gate, order submission) is delegated.
        """
        try:
            ticker = trade.symbol
            price = float(trade.price)
            now = time.time()

            # Update cache
            self._last_prices[ticker] = price
            self._stats['ticks_received'] += 1
            self._stats['last_tick_at'] = datetime.now().isoformat()
            self._tick_window.append(now)

            # Trim tick window to last 60 seconds
            if len(self._tick_window) > 5000:
                cutoff = now - 60
                self._tick_window = [t for t in self._tick_window if t > cutoff]

            # ── CHECK 1: Entry trigger (conditional tracking) ─────────────
            if ticker in self._conditionals:
                self._check_entry_trigger(ticker, price, now)

            # ── CHECK 2: Exit monitoring (open positions) ─────────────────
            if ticker in self._positions:
                self._check_exit_conditions(ticker, price, now)

        except Exception as e:
            self._stats['errors'] += 1
            self._stats['last_error'] = f"tick handler: {e}"
            # Never let a single tick error kill the stream
            logger.debug(f"🔴 Tick handler error for {trade.symbol}: {e}")

    # ── Entry Trigger Logic ───────────────────────────────────────────────────

    def _check_entry_trigger(self, ticker: str, price: float, now: float):
        """Check if price has hit the conditional entry target."""
        cond = self._conditionals.get(ticker)
        if not cond:
            return

        entry_target = cond.get('entry_price_target')
        if not entry_target:
            return

        watch_tag = (cond.get('watch_tag') or 'untagged').lower()
        is_upside = watch_tag in UPSIDE_TRIGGER_TAGS

        # Proximity alert (within 2% of target)
        if is_upside:
            # Breakout: approaching target from below
            if price >= entry_target * (1 - PROXIMITY_ALERT_PCT) and price < entry_target:
                dist_pct = (entry_target - price) / entry_target * 100
                last_prox_key = f"prox_{ticker}"
                last_prox = self._last_trigger_time.get(last_prox_key, 0)
                if now - last_prox > 60:
                    self._last_trigger_time[last_prox_key] = now
                    self._stats['proximity_alerts'] += 1
                    logger.info(
                        f"📍 PROXIMITY: {ticker} ${price:.2f} — {dist_pct:.1f}% below "
                        f"breakout target ${entry_target:.2f} [{watch_tag}]"
                    )
        else:
            # Pullback: approaching target from above
            if price <= entry_target * (1 + PROXIMITY_ALERT_PCT) and price > entry_target:
                dist_pct = (price - entry_target) / entry_target * 100
                last_prox_key = f"prox_{ticker}"
                last_prox = self._last_trigger_time.get(last_prox_key, 0)
                if now - last_prox > 60:
                    self._last_trigger_time[last_prox_key] = now
                    self._stats['proximity_alerts'] += 1
                    logger.info(
                        f"📍 PROXIMITY: {ticker} ${price:.2f} — {dist_pct:.1f}% from "
                        f"entry target ${entry_target:.2f} [{watch_tag}]"
                    )

        # Entry trigger: direction depends on watch_tag
        if is_upside:
            # Breakout: price at or above target, capped at 10% extension
            max_price = entry_target * (1 + BREAKOUT_MAX_EXTENSION)
            triggered = price >= entry_target and price <= max_price
        else:
            # Pullback: price at or below target
            triggered = price <= entry_target

        if triggered:
            # Debounce: don't re-trigger within DEBOUNCE_TRIGGER_SEC
            last_trigger = self._last_trigger_time.get(ticker, 0)
            if now - last_trigger < DEBOUNCE_TRIGGER_SEC:
                return

            self._last_trigger_time[ticker] = now
            self._stats['entry_triggers'] += 1
            self._stats['last_trigger_at'] = datetime.now().isoformat()
            self._stats['last_trigger_ticker'] = ticker
            self._stats['last_trigger_type'] = 'entry'

            if is_upside:
                trigger_desc = f"${price:.2f} >= target ${entry_target:.2f} (breakout)"
            else:
                trigger_desc = f"${price:.2f} <= target ${entry_target:.2f}"
            logger.info(
                f"🎯 ENTRY TRIGGER: {ticker} [{watch_tag}] "
                f"{trigger_desc} — dispatching to broker"
            )

            # Fire the broker's existing conditional processing
            # This runs the full pipeline: Pro gate → size → execute/notify
            self._dispatch_entry_trigger(ticker, price, cond)

    def _dispatch_entry_trigger(self, ticker: str, price: float, cond: dict):
        """
        Dispatch entry trigger to the broker in a separate thread
        so we don't block the WebSocket handler.

        Uses a per-ticker lock to prevent concurrent gate calls — without this,
        the 5s debounce + ~20s Gemini Pro gate creates a race where multiple
        threads all read the conditional as 'active' before any commit lands,
        causing duplicate Pro calls and duplicate Slack notifications.
        """
        if not self.broker:
            logger.warning(f"🎯 {ticker} triggered but no broker attached — skipping")
            return

        def _run():
            # Per-ticker lock: only one gate call at a time per conditional
            lock = self._dispatch_locks[ticker]
            if not lock.acquire(blocking=False):
                logger.debug(f"🎯 {ticker} dispatch already in progress — skipping duplicate")
                return
            try:
                # Build live_state from latest available data
                live_state = self._build_live_state()

                # Process just this one conditional via the broker's full pipeline
                # The broker handles: Pro gate, sizing, exposure cap, execution/notification
                entries = self.broker.process_acquisition_conditionals(live_state=live_state)
                if entries > 0:
                    logger.info(f"🎯 BROKER: {entries} acquisition conditional(s) entered via real-time trigger")

                    # Refresh subscriptions — the triggered conditional may now be
                    # 'triggered' status and the new position needs exit monitoring
                    self._refresh_subscriptions()

                    # Notify #logs-silent
                    try:
                        from alerts import AlertSystem
                        AlertSystem().send_silent_log('realtime_entry_trigger', {
                            'ticker': ticker,
                            'trigger_price': price,
                            'entry_target': cond.get('entry_price_target'),
                            'watch_tag': cond.get('watch_tag', ''),
                            'entries_executed': entries,
                        })
                    except Exception:
                        pass

            except Exception as e:
                logger.error(f"🔴 Entry trigger dispatch failed for {ticker}: {e}")
                self._stats['errors'] += 1
                self._stats['last_error'] = f"dispatch {ticker}: {e}"
            finally:
                lock.release()

        threading.Thread(target=_run, daemon=True, name=f'trigger-{ticker}').start()

    # ── Exit Condition Logic ──────────────────────────────────────────────────

    def _check_exit_conditions(self, ticker: str, price: float, now: float):
        """
        Real-time exit monitoring for open positions.

        Updates peak_price on every tick (critical for trailing stop accuracy).
        Fires exit alerts when thresholds are crossed.
        """
        pos = self._positions.get(ticker)
        if not pos:
            return

        entry_price = pos.get('entry_price', 0)
        peak_price = pos.get('peak_price') or entry_price
        thesis_floor = pos.get('stop_loss')         # Analyst's hard invalidation floor
        tp1 = pos.get('take_profit_1')
        trade_id = pos.get('trade_id')

        if not entry_price or entry_price <= 0:
            return

        # ── ALWAYS: Update peak price (high-watermark) ────────────────────
        if price > peak_price:
            self._positions[ticker]['peak_price'] = price
            self._stats['peak_updates'] += 1

            # Persist to DB (batch-friendly: only write if significant change)
            if price > peak_price * 1.001:  # >0.1% new high
                self._update_peak_in_db(trade_id, price)
                peak_price = price  # Use new peak for trailing calc below

        # Calculate trailing stop
        trailing_stop_px = round(peak_price * (1 - TRAILING_STOP_PCT), 4)

        # ── CHECK: Thesis floor breached (immediate, no gate) ─────────────
        if thesis_floor and price < thesis_floor:
            last_exit = self._last_trigger_time.get(f"exit_{ticker}", 0)
            if now - last_exit < DEBOUNCE_TRIGGER_SEC:
                return
            self._last_trigger_time[f"exit_{ticker}"] = now
            self._stats['exit_triggers'] += 1
            self._stats['last_trigger_at'] = datetime.now().isoformat()
            self._stats['last_trigger_ticker'] = ticker
            self._stats['last_trigger_type'] = 'thesis_floor'

            pnl_pct = (price - entry_price) / entry_price * 100
            logger.warning(
                f"🚨 THESIS FLOOR: {ticker} ${price:.2f} < ${thesis_floor:.2f} "
                f"(PnL: {pnl_pct:+.1f}%) — dispatching exit"
            )
            self._dispatch_exit_check(ticker, price, 'thesis_floor')
            return

        # ── CHECK: Trailing stop breached ─────────────────────────────────
        if price < trailing_stop_px:
            last_exit = self._last_trigger_time.get(f"exit_{ticker}", 0)
            if now - last_exit < DEBOUNCE_TRIGGER_SEC:
                return
            self._last_trigger_time[f"exit_{ticker}"] = now
            self._stats['exit_triggers'] += 1
            self._stats['last_trigger_at'] = datetime.now().isoformat()
            self._stats['last_trigger_ticker'] = ticker
            self._stats['last_trigger_type'] = 'trailing_stop'

            pnl_pct = (price - entry_price) / entry_price * 100
            logger.warning(
                f"🛑 TRAILING STOP: {ticker} ${price:.2f} < ${trailing_stop_px:.2f} "
                f"(peak ${peak_price:.2f}, PnL: {pnl_pct:+.1f}%) — dispatching exit"
            )
            self._dispatch_exit_check(ticker, price, 'trailing_stop')
            return

        # ── CHECK: Take profit hit ────────────────────────────────────────
        if tp1 and price >= tp1:
            last_exit = self._last_trigger_time.get(f"exit_{ticker}", 0)
            if now - last_exit < DEBOUNCE_TRIGGER_SEC:
                return
            self._last_trigger_time[f"exit_{ticker}"] = now
            self._stats['exit_triggers'] += 1
            self._stats['last_trigger_at'] = datetime.now().isoformat()
            self._stats['last_trigger_ticker'] = ticker
            self._stats['last_trigger_type'] = 'take_profit'

            pnl_pct = (price - entry_price) / entry_price * 100
            logger.info(
                f"📈 TAKE PROFIT: {ticker} ${price:.2f} >= TP1 ${tp1:.2f} "
                f"(PnL: +{pnl_pct:.1f}%) — dispatching exit"
            )
            self._dispatch_exit_check(ticker, price, 'take_profit')
            return

    def _dispatch_exit_check(self, ticker: str, price: float, trigger_type: str):
        """
        Dispatch exit to broker in a separate thread.
        The broker's process_exits() handles the full exit pipeline
        including pre-exit gate for trailing stops.
        """
        if not self.broker:
            logger.warning(f"🛑 {ticker} exit triggered but no broker attached")
            return

        def _run():
            try:
                # Update the current price in DB so process_exits() sees it
                self._update_current_price_in_db(ticker, price)

                exits = self.broker.process_exits()
                if exits > 0:
                    logger.info(f"🛑 BROKER: {exits} position(s) exited via real-time {trigger_type} trigger")
                    # Refresh subscriptions — position may be closed now
                    self._refresh_subscriptions()

                    try:
                        from alerts import AlertSystem
                        AlertSystem().send_silent_log('realtime_exit_trigger', {
                            'ticker': ticker,
                            'trigger_price': price,
                            'trigger_type': trigger_type,
                            'exits_executed': exits,
                        })
                    except Exception:
                        pass
            except Exception as e:
                logger.error(f"🔴 Exit dispatch failed for {ticker}: {e}")
                self._stats['errors'] += 1

        threading.Thread(target=_run, daemon=True, name=f'exit-{ticker}').start()

    # ── DB Helpers ────────────────────────────────────────────────────────────

    def _update_peak_in_db(self, trade_id: int, new_peak: float):
        """Write new peak_price to trade_records."""
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=3)
            conn.execute("""
                UPDATE trade_records
                SET peak_price = ?, last_price_updated = ?
                WHERE trade_id = ? AND status = 'open'
            """, (new_peak, datetime.now().isoformat(), trade_id))
            conn.commit()
            conn.close()
        except Exception:
            pass  # Non-critical — will be retried on next tick

    def _update_current_price_in_db(self, ticker: str, price: float):
        """Write current price + PnL to trade_records so process_exits() sees fresh data."""
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=3)
            conn.execute("""
                UPDATE trade_records
                SET current_price = ?,
                    unrealized_pnl_dollars = (? - entry_price) * shares,
                    unrealized_pnl_percent = (? - entry_price) / entry_price * 100,
                    last_price_updated = ?,
                    peak_price = MAX(COALESCE(peak_price, entry_price), ?)
                WHERE asset_symbol = ? AND status = 'open'
            """, (price, price, price, datetime.now().isoformat(), price, ticker))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.debug(f"DB price update failed for {ticker}: {e}")

    def _write_health_to_db(self, status: dict):
        """Persist stream health snapshot for the dashboard."""
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=3)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS stream_health (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp   TEXT NOT NULL,
                    status      TEXT,
                    ticks       INTEGER,
                    tps         REAL,
                    tickers     INTEGER,
                    entries     INTEGER,
                    exits       INTEGER,
                    peaks       INTEGER,
                    errors      INTEGER,
                    feed        TEXT,
                    details_json TEXT
                )
            """)
            conn.execute("""
                INSERT INTO stream_health
                  (timestamp, status, ticks, tps, tickers, entries, exits, peaks, errors, feed, details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now().isoformat(),
                status.get('status', ''),
                status.get('ticks_received', 0),
                status.get('ticks_per_sec', 0),
                status.get('subscribed_tickers', 0),
                status.get('entry_triggers', 0),
                status.get('exit_triggers', 0),
                status.get('peak_updates', 0),
                status.get('errors', 0),
                status.get('feed', ''),
                json.dumps({
                    'subscribed_list': status.get('subscribed_list', []),
                    'conditionals_watched': status.get('conditionals_watched', 0),
                    'positions_watched': status.get('positions_watched', 0),
                    'reconnects': status.get('reconnects', 0),
                    'last_error': status.get('last_error'),
                    'last_tick_at': status.get('last_tick_at'),
                    'last_trigger_at': status.get('last_trigger_at'),
                    'last_trigger_ticker': status.get('last_trigger_ticker'),
                    'last_trigger_type': status.get('last_trigger_type'),
                }),
            ))
            # Keep only last 24h of health records
            conn.execute("""
                DELETE FROM stream_health
                WHERE timestamp < datetime('now', '-1 day')
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.debug(f"Health DB write failed: {e}")

    def _build_live_state(self) -> dict:
        """Build a live_state dict for the broker from latest available data."""
        live_state = {}
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=3)

            # Latest DEFCON
            row = conn.execute(
                "SELECT defcon_level FROM signal_monitoring ORDER BY monitor_id DESC LIMIT 1"
            ).fetchone()
            live_state['defcon'] = row[0] if row else 5

            # Latest macro score
            row = conn.execute(
                "SELECT macro_score FROM macro_indicators ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            live_state['macro_score'] = float(row[0]) if row else 50.0

            # Latest news score
            row = conn.execute(
                "SELECT news_score FROM news_signals ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            live_state['news_score'] = float(row[0]) if row else 0

            # VIX from our cache if available
            vix_price = self._last_prices.get('^VIX')
            if vix_price:
                live_state['vix'] = vix_price

            conn.close()
        except Exception:
            live_state.setdefault('defcon', 5)
            live_state.setdefault('macro_score', 50.0)
            live_state.setdefault('news_score', 0)

        return live_state


# ── Standalone test ───────────────────────────────────────────────────────────

def _test_stream():
    """Quick smoke test — subscribe to a few tickers and print ticks for 30s."""
    import sys
    from dotenv import load_dotenv
    load_dotenv(SCRIPT_DIR / '.env')

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    monitor = RealtimeMonitor()
    if not monitor._configured:
        print("ERROR: ALPACA_API_KEY / ALPACA_SECRET_KEY not set in .env")
        sys.exit(1)

    print(f"Starting stream test ({monitor._feed} feed)...")
    monitor.start()

    try:
        for i in range(60):
            time.sleep(1)
            status = monitor.get_status()
            if i % 5 == 0:
                print(
                    f"  [{i}s] status={status['status']} ticks={status['ticks_received']} "
                    f"tps={status['ticks_per_sec']} tickers={status['subscribed_tickers']}"
                )
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop()
        print(f"\nFinal: {monitor.get_status()}")


if __name__ == '__main__':
    _test_stream()
