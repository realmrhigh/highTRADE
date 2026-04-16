"""
trade_thesis.py — Entry thesis metadata preservation and anti-reentry gating.

Three responsibilities:
  1. Schema migration — adds thesis columns to trade_records and creates the
     thesis_invalidation_log table the first time the module is imported.
  2. save_entry_thesis() — called at trade open to snapshot the reasoning that
     justified the entry (signal breakdown, catalyst, regime context).
  3. Anti-reentry gating — check_reentry_allowed() blocks or warns when the
     system tries to re-enter a ticker that was recently stopped-out or
     thesis-invalidated, unless a genuinely new catalyst is presented.

This module is deliberately side-effect-free except for the lightweight DDL
that runs once on import.  It never touches live orders.

Usage
-----
    from trade_thesis import save_entry_thesis, check_reentry_allowed, record_thesis_invalidation

    # At buy time:
    save_entry_thesis(conn, trade_id, thesis_meta)

    # Before any new entry on the same ticker:
    ok, reason = check_reentry_allowed(conn, ticker)
    if not ok:
        logger.warning(reason); return

    # When a stop-loss or invalidation exit fires:
    record_thesis_invalidation(conn, ticker, trade_id, exit_reason, exit_price,
                                catalyst_event=..., thesis_summary=...)
"""

import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# ── Reentry cooldown configuration ────────────────────────────────────────────
# After a stop-loss or invalidation exit the ticker is gated for this many hours.
# A *new* catalyst (a different catalyst_event string) can override the soft gate.
REENTRY_COOLDOWN_HOURS_STOP_LOSS    = 24   # 1 trading day
REENTRY_COOLDOWN_HOURS_INVALIDATION = 48   # 2 trading days
# After this many stop-outs in the rolling window, the gate becomes HARD (no override)
HARD_BLOCK_STOP_THRESHOLD = 2
HARD_BLOCK_WINDOW_DAYS    = 7


# ── DDL ───────────────────────────────────────────────────────────────────────

_DDL_TRADE_RECORDS_COLUMNS = [
    ("entry_thesis_text",    "TEXT",    "Entry thesis / reasoning chain at time of buy"),
    ("entry_catalyst_text",  "TEXT",    "Catalyst description at entry"),
    ("entry_signal_breakdown","TEXT",   "JSON: per-signal score breakdown at entry"),
    ("entry_regime_context", "TEXT",    "Market regime / DEFCON context at entry"),
    ("entry_discovery_score","REAL",    "Discovery score 0-100 at entry"),
    ("entry_catalyst_score", "REAL",    "Catalyst score 0-100 at entry"),
    ("entry_regime_score",   "REAL",    "Regime score 0-100 at entry"),
    ("entry_conviction",     "REAL",    "Overall conviction score 0-100 at entry"),
]

_DDL_CREATE_INVALIDATION_LOG = """
CREATE TABLE IF NOT EXISTS thesis_invalidation_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ticker              TEXT NOT NULL,
    trade_id            INTEGER,
    exit_reason         TEXT,           -- 'stop_loss' | 'invalidation' | 'manual'
    exit_price          REAL,
    entry_price         REAL,
    loss_dollars        REAL,
    loss_pct            REAL,
    catalyst_event      TEXT,           -- catalyst that justified the failed entry
    thesis_summary      TEXT,           -- brief thesis snapshot
    blocked_until       TIMESTAMP,      -- hard block expires after this time
    block_type          TEXT,           -- 'soft' | 'hard'
    override_allowed    INTEGER DEFAULT 1,  -- 0 = hard block, no override
    new_catalyst_needed TEXT,           -- hint about what would unlock re-entry
    notes               TEXT
)
"""

_DDL_INDEX_INVALIDATION = """
CREATE INDEX IF NOT EXISTS idx_thesis_invalidation_ticker
    ON thesis_invalidation_log (ticker, logged_at DESC)
"""


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Add thesis columns to trade_records and create thesis_invalidation_log.
    Safe to call multiple times — all ops are idempotent."""
    cur = conn.cursor()

    # Add thesis columns to trade_records (ALTER TABLE IF NOT EXISTS equivalent)
    existing = {row[1] for row in cur.execute("PRAGMA table_info(trade_records)").fetchall()}
    for col_name, col_type, _ in _DDL_TRADE_RECORDS_COLUMNS:
        if col_name not in existing:
            try:
                cur.execute(f"ALTER TABLE trade_records ADD COLUMN {col_name} {col_type}")
                logger.info("trade_thesis: added column trade_records.%s", col_name)
            except sqlite3.OperationalError as e:
                logger.debug("trade_thesis: column add skipped (%s): %s", col_name, e)

    # Create invalidation log table
    cur.execute(_DDL_CREATE_INVALIDATION_LOG)
    cur.execute(_DDL_INDEX_INVALIDATION)
    conn.commit()
    logger.debug("trade_thesis: schema migration complete")


# ── Public API ────────────────────────────────────────────────────────────────

def save_entry_thesis(
    conn: sqlite3.Connection,
    trade_id: int,
    *,
    thesis_text: str = "",
    catalyst_text: str = "",
    signal_breakdown: str = "",     # JSON string of per-signal scores
    regime_context: str = "",
    discovery_score: Optional[float] = None,
    catalyst_score: Optional[float] = None,
    regime_score: Optional[float] = None,
    conviction: Optional[float] = None,
    signal_score: Optional[float] = None,   # top-level signal score (writes entry_signal_score too)
) -> bool:
    """
    Persist entry thesis metadata to trade_records row identified by trade_id.

    Returns True on success, False on any error (never raises).
    """
    try:
        _migrate_schema(conn)
        updates = {}
        if thesis_text:
            updates["entry_thesis_text"]     = thesis_text
        if catalyst_text:
            updates["entry_catalyst_text"]   = catalyst_text
        if signal_breakdown:
            updates["entry_signal_breakdown"] = signal_breakdown
        if regime_context:
            updates["entry_regime_context"]  = regime_context
        if discovery_score is not None:
            updates["entry_discovery_score"] = discovery_score
        if catalyst_score is not None:
            updates["entry_catalyst_score"]  = catalyst_score
        if regime_score is not None:
            updates["entry_regime_score"]    = regime_score
        if conviction is not None:
            updates["entry_conviction"]      = conviction
        if signal_score is not None:
            updates["entry_signal_score"]    = signal_score

        if not updates:
            return True  # nothing to write

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [trade_id]
        conn.execute(f"UPDATE trade_records SET {set_clause} WHERE trade_id = ?", values)
        conn.commit()
        logger.info("trade_thesis: thesis saved for trade_id=%d (fields: %s)", trade_id, list(updates.keys()))
        return True
    except Exception as e:
        logger.warning("trade_thesis: save_entry_thesis failed for trade_id=%s: %s", trade_id, e)
        return False


def record_thesis_invalidation(
    conn: sqlite3.Connection,
    ticker: str,
    trade_id: Optional[int],
    exit_reason: str,           # 'stop_loss' | 'invalidation' | 'manual'
    exit_price: float,
    entry_price: float = 0.0,
    catalyst_event: str = "",
    thesis_summary: str = "",
    notes: str = "",
) -> None:
    """
    Log a thesis invalidation event and set a reentry cooldown for the ticker.

    Called immediately after a stop-loss or thesis-invalidation exit fires.
    Never raises — errors are logged and swallowed.
    """
    try:
        _migrate_schema(conn)

        loss_dollars = (exit_price - entry_price) * 1 if entry_price else 0  # 1 share equiv for pct calc
        loss_pct = ((exit_price - entry_price) / entry_price * 100) if entry_price else 0

        if exit_reason == 'stop_loss':
            cooldown_hours = REENTRY_COOLDOWN_HOURS_STOP_LOSS
        else:
            cooldown_hours = REENTRY_COOLDOWN_HOURS_INVALIDATION

        blocked_until = datetime.now() + timedelta(hours=cooldown_hours)

        # Determine if this is a hard block (too many recent stop-outs).
        # Count existing stops; this new one will be stop #(recent_stops+1).
        recent_stops = _count_recent_stops(conn, ticker)
        is_hard = (recent_stops + 1) >= HARD_BLOCK_STOP_THRESHOLD if exit_reason == 'stop_loss' else False
        block_type = 'hard' if is_hard else 'soft'
        override_allowed = 0 if is_hard else 1

        new_catalyst_needed = (
            "HARD BLOCK — manual review required (too many recent stop-outs on this ticker)"
            if is_hard else
            f"New catalyst required (different from: '{catalyst_event or 'unknown'}')"
        )

        conn.execute("""
            INSERT INTO thesis_invalidation_log
            (ticker, trade_id, exit_reason, exit_price, entry_price,
             loss_dollars, loss_pct, catalyst_event, thesis_summary,
             blocked_until, block_type, override_allowed, new_catalyst_needed, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            ticker, trade_id, exit_reason, exit_price, entry_price,
            loss_dollars, loss_pct, catalyst_event, thesis_summary,
            blocked_until.isoformat(), block_type, override_allowed,
            new_catalyst_needed, notes,
        ))
        conn.commit()

        logger.info(
            "trade_thesis: invalidation logged for %s (trade_id=%s, reason=%s, "
            "block=%s until %s)",
            ticker, trade_id, exit_reason, block_type,
            blocked_until.strftime('%Y-%m-%d %H:%M'),
        )
    except Exception as e:
        logger.warning("trade_thesis: record_thesis_invalidation failed for %s: %s", ticker, e)


def check_reentry_allowed(
    conn: sqlite3.Connection,
    ticker: str,
    new_catalyst: str = "",
) -> Tuple[bool, str]:
    """
    Check whether a new entry on *ticker* is currently allowed.

    Returns (allowed: bool, reason: str).

    Logic:
    - If no active block → allowed.
    - If hard block (override_allowed=0) → denied regardless of catalyst.
    - If soft block and new_catalyst differs from the blocked catalyst → allowed
      (the new setup is based on different information).
    - If soft block and same (or empty) catalyst → denied.
    """
    try:
        _migrate_schema(conn)

        now = datetime.now()
        rows = conn.execute("""
            SELECT id, logged_at, exit_reason, catalyst_event,
                   blocked_until, block_type, override_allowed, new_catalyst_needed
            FROM thesis_invalidation_log
            WHERE ticker = ?
              AND blocked_until > ?
            ORDER BY logged_at DESC
            LIMIT 5
        """, (ticker, now.isoformat())).fetchall()

        if not rows:
            return True, "no active block"

        # Collect the most restrictive active block
        hard_blocks = [r for r in rows if r[6] == 0]  # override_allowed = 0
        soft_blocks = [r for r in rows if r[6] == 1]

        if hard_blocks:
            latest = hard_blocks[0]
            blocked_until = latest[4]
            return False, (
                f"HARD BLOCK on {ticker}: {latest[3] or latest[1]} — "
                f"too many recent stop-outs. Block expires {blocked_until[:16]}. "
                f"Manual review required."
            )

        if soft_blocks:
            latest = soft_blocks[0]
            old_catalyst = (latest[3] or "").strip().lower()
            new_cat_norm = (new_catalyst or "").strip().lower()
            blocked_until = latest[4]

            # Allow if genuinely different catalyst
            if new_cat_norm and old_catalyst and new_cat_norm != old_catalyst:
                # Quick sanity: reject if they share most words (same story reworded)
                old_words = set(old_catalyst.split())
                new_words = set(new_cat_norm.split())
                if len(old_words) > 0:
                    overlap = len(old_words & new_words) / len(old_words)
                    if overlap < 0.6:  # less than 60% word overlap → new catalyst
                        return True, f"soft block overridden by new catalyst: '{new_catalyst}'"

            return False, (
                f"SOFT BLOCK on {ticker} (exit: {latest[2]}, "
                f"original catalyst: '{latest[3] or 'unknown'}') — "
                f"blocked until {blocked_until[:16]}. "
                f"{latest[7] or 'Provide a different catalyst to override.'}"
            )

        return True, "no active block"

    except Exception as e:
        # Fail-open: if the check itself errors, don't block trading
        logger.warning("trade_thesis: check_reentry_allowed error for %s: %s", ticker, e)
        return True, f"gate check error (fail-open): {e}"


def get_invalidation_summary(conn: sqlite3.Connection, ticker: str, days: int = 30) -> dict:
    """
    Return a summary dict for use in postmortems and dashboard.

    {
      'ticker': str,
      'stop_outs_30d': int,
      'invalidations_30d': int,
      'total_loss_dollars': float,
      'active_block': bool,
      'block_type': str | None,
      'block_expires': str | None,
    }
    """
    try:
        _migrate_schema(conn)
        since = (datetime.now() - timedelta(days=days)).isoformat()
        now = datetime.now().isoformat()

        rows = conn.execute("""
            SELECT exit_reason, loss_dollars, blocked_until, block_type, override_allowed
            FROM thesis_invalidation_log
            WHERE ticker = ? AND logged_at >= ?
            ORDER BY logged_at DESC
        """, (ticker, since)).fetchall()

        stop_outs = sum(1 for r in rows if r[0] == 'stop_loss')
        invalidations = sum(1 for r in rows if r[0] == 'invalidation')
        total_loss = sum(r[1] or 0 for r in rows)

        active = [r for r in rows if r[2] and r[2] > now]
        block_type = active[0][3] if active else None
        block_expires = active[0][2][:16] if active else None

        return {
            'ticker': ticker,
            'stop_outs_30d': stop_outs,
            'invalidations_30d': invalidations,
            'total_loss_dollars': total_loss,
            'active_block': bool(active),
            'block_type': block_type,
            'block_expires': block_expires,
        }
    except Exception as e:
        logger.warning("trade_thesis: get_invalidation_summary error: %s", e)
        return {'ticker': ticker, 'error': str(e)}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _count_recent_stops(conn: sqlite3.Connection, ticker: str) -> int:
    """Count stop-loss exits for ticker in the last HARD_BLOCK_WINDOW_DAYS days."""
    since = (datetime.now() - timedelta(days=HARD_BLOCK_WINDOW_DAYS)).isoformat()
    try:
        row = conn.execute("""
            SELECT COUNT(*) FROM thesis_invalidation_log
            WHERE ticker = ? AND exit_reason = 'stop_loss' AND logged_at >= ?
        """, (ticker, since)).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


# ── Self-test ─────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import tempfile, os
    logging.basicConfig(level=logging.DEBUG)

    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        tmp = f.name

    conn = sqlite3.connect(tmp)
    conn.execute("""
        CREATE TABLE trade_records (
            trade_id INTEGER PRIMARY KEY,
            asset_symbol TEXT,
            entry_signal_score REAL
        )
    """)
    conn.execute("INSERT INTO trade_records (trade_id, asset_symbol) VALUES (1, 'TEST')")
    conn.commit()

    # Test thesis save
    ok = save_entry_thesis(conn, 1, thesis_text="Test thesis", signal_score=72.5,
                            catalyst_text="Q1 earnings beat", conviction=72.5)
    assert ok, "save_entry_thesis failed"

    # Test no block
    allowed, reason = check_reentry_allowed(conn, 'TEST')
    assert allowed, f"expected allowed, got: {reason}"

    # Record a stop-loss
    record_thesis_invalidation(conn, 'TEST', 1, 'stop_loss', 45.0, 48.0,
                                catalyst_event='Q1 earnings beat', thesis_summary='Earnings play')

    # Soft block in effect
    allowed, reason = check_reentry_allowed(conn, 'TEST')
    assert not allowed, f"expected blocked, got: {reason}"

    # Different catalyst overrides soft block
    allowed, reason = check_reentry_allowed(conn, 'TEST', new_catalyst='FDA approval announcement')
    assert allowed, f"expected override, got: {reason}"

    # Second stop-loss → hard block
    record_thesis_invalidation(conn, 'TEST', 1, 'stop_loss', 44.0, 48.0)
    allowed, reason = check_reentry_allowed(conn, 'TEST', new_catalyst='FDA approval announcement')
    assert not allowed, f"expected hard block, got: {reason}"

    conn.close()
    os.unlink(tmp)
    print("✅ trade_thesis self-test passed")
