#!/usr/bin/env python3
"""
ai_choreographer.py — Cross-process AI API rate limiter for highTRADE / highCRYPTO

Coordinates Gemini and Grok API calls across multiple simultaneously-running
trading system instances using:
  - Shared SQLite   (~/.hightrade/ai_quota_shared.db)  for cross-system call history
  - fcntl file lock (~/.hightrade/locks/{model}.lock)  for atomic read-check-write

Race condition prevented:
  Without choreographer:  orchestratorA reads "last_call=5s ago", orchestratorB reads
                          same → both sleep 0s → both fire simultaneously → 2× RPM burst.
  With choreographer:     orchestratorB blocks on fcntl.LOCK_EX until A finishes writing
                          its timestamp → B sees fresh timestamp → B sleeps the correct delta.

Usage:
    from ai_choreographer import AIChoreographer
    AIChoreographer.pace_and_record('gemini-3.1-pro-preview', 'highcrypto')
    # blocks if needed, then records call — safe to call from any thread/process
"""

import fcntl
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# ── Shared state location (outside both project directories) ─────────────────
_HOME      = Path.home() / ".hightrade"
_SHARED_DB = _HOME / "ai_quota_shared.db"
_LOCK_DIR  = _HOME / "locks"

# ── Per-model limits — err slightly conservative vs. actual API ceilings ──────
MODEL_LIMITS: Dict[str, Dict[str, int]] = {
    # Gemini
    "gemini-3.1-pro-preview":        {"rpm": 25,  "rpd": 250},
    "gemini-3-flash-preview":        {"rpm": 120, "rpd": 1500},
    "gemini-2.5-pro":                {"rpm": 120, "rpd": 1500},
    "gemini-3.1-flash-lite-preview": {"rpm": 120, "rpd": 4000},
    # Grok (xAI limits not officially published — conservative default)
    "grok-4-1-fast-reasoning":       {"rpm": 60,  "rpd": 9999},
    "grok-3-mini":                   {"rpm": 60,  "rpd": 9999},
}

_DEFAULT_RPM = 30
_DEFAULT_RPD = 1000

_QUOTA_WARN_PCT  = 0.75
_QUOTA_BLOCK_PCT = 0.90

# ── Per-model thread locks (prevents same-process thread races before fcntl) ──
_thread_locks: Dict[str, threading.Lock] = {}
_thread_locks_meta = threading.Lock()


def _get_thread_lock(model_id: str) -> threading.Lock:
    with _thread_locks_meta:
        if model_id not in _thread_locks:
            _thread_locks[model_id] = threading.Lock()
        return _thread_locks[model_id]


def _lock_filename(model_id: str) -> Path:
    """Sanitize model_id into a safe filename."""
    safe = model_id.replace("/", "_").replace("-", "_").replace(".", "_")
    return _LOCK_DIR / f"{safe}.lock"


class AIChoreographer:
    """
    Cross-process API rate limiter.  All public methods are classmethods.

    Designed to be safe to call on every API invocation — if the shared DB is
    unavailable for any reason the class logs a warning and returns immediately,
    never crashing the trading orchestrator.
    """

    _db_initialized = False
    _db_init_lock   = threading.Lock()

    # ── Initialisation ────────────────────────────────────────────────────────

    @classmethod
    def _ensure_db(cls) -> None:
        """Create shared DB, lock dir, and schema on first use.  Silent on error."""
        if cls._db_initialized:
            return
        with cls._db_init_lock:
            if cls._db_initialized:
                return
            try:
                _HOME.mkdir(parents=True, exist_ok=True)
                _LOCK_DIR.mkdir(parents=True, exist_ok=True)

                conn = sqlite3.connect(str(_SHARED_DB), timeout=10)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS api_calls (
                        id       INTEGER PRIMARY KEY AUTOINCREMENT,
                        model_id TEXT    NOT NULL,
                        system   TEXT    NOT NULL DEFAULT 'unknown',
                        ts       REAL    NOT NULL
                    )
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_model_ts ON api_calls(model_id, ts)"
                )
                # Prune records older than 7 days to keep the DB small
                cutoff = time.time() - (7 * 86400)
                conn.execute("DELETE FROM api_calls WHERE ts < ?", (cutoff,))
                conn.commit()
                conn.close()
                cls._db_initialized = True
                logger.info(
                    f"[AIChoreographer] Shared quota DB ready: {_SHARED_DB}"
                )
            except Exception as exc:
                logger.warning(
                    f"[AIChoreographer] DB init failed — cross-process pacing disabled: {exc}"
                )

    # ── Core pacing method ────────────────────────────────────────────────────

    @classmethod
    def pace_and_record(cls, model_id: str, system_name: str = "hightrade") -> None:
        """
        Block until it is safe to call model_id, then atomically record the call.

        Locking sequence
        ────────────────
        1. Acquire threading.Lock   → stops same-process threads from racing each other
        2. Acquire fcntl.LOCK_EX    → stops other OS processes from racing us
        3. Read MAX(ts) from shared DB
        4. Compute wait = min_interval − (now − last_ts); sleep if > 0
        5. INSERT new row with ts = time.time()
        6. Commit + close DB
        7. Release fcntl lock → next waiter now sees the fresh timestamp
        8. Release thread lock

        The file lock is intentionally held during the sleep so that any other
        waiter (from any process) sees the correct last-call time the moment
        it acquires the lock, without reading stale data.
        """
        cls._ensure_db()
        if not cls._db_initialized:
            return  # DB down — skip gracefully, don't crash caller

        limits       = MODEL_LIMITS.get(model_id, {})
        rpm          = limits.get("rpm", _DEFAULT_RPM)
        min_interval = 60.0 / rpm

        lock_path    = _lock_filename(model_id)
        thread_lock  = _get_thread_lock(model_id)

        with thread_lock:
            try:
                lf = open(str(lock_path), "w")
                try:
                    fcntl.flock(lf, fcntl.LOCK_EX)   # blocks until we own the lock

                    # ── Critical section ──────────────────────────────────────
                    conn = sqlite3.connect(str(_SHARED_DB), timeout=10)
                    conn.execute("PRAGMA journal_mode=WAL")

                    row     = conn.execute(
                        "SELECT MAX(ts) FROM api_calls WHERE model_id = ?",
                        (model_id,)
                    ).fetchone()
                    last_ts = row[0] if (row and row[0] is not None) else 0.0
                    now     = time.time()
                    wait    = min_interval - (now - last_ts)

                    if wait > 0.0:
                        logger.debug(
                            f"[AIChoreographer] {model_id} — sleeping {wait:.2f}s "
                            f"(rpm={rpm}, caller={system_name})"
                        )
                        time.sleep(wait)

                    conn.execute(
                        "INSERT INTO api_calls(model_id, system, ts) VALUES (?, ?, ?)",
                        (model_id, system_name, time.time()),
                    )
                    conn.commit()
                    conn.close()
                    # ── End critical section ──────────────────────────────────

                finally:
                    fcntl.flock(lf, fcntl.LOCK_UN)
                    lf.close()

            except Exception as exc:
                logger.warning(
                    f"[AIChoreographer] pace_and_record error ({model_id}): {exc}"
                )

    # ── Daily quota check ─────────────────────────────────────────────────────

    @classmethod
    def check_daily_quota(cls, model_id: str) -> str:
        """
        Returns 'ok', 'warn', or 'block' based on COMBINED call count from ALL
        systems since last UTC midnight.

        Thresholds: warn ≥ 75 %, block ≥ 90 %.
        """
        cls._ensure_db()
        if not cls._db_initialized:
            return "ok"  # DB down — let caller proceed

        limits = MODEL_LIMITS.get(model_id, {})
        rpd    = limits.get("rpd", _DEFAULT_RPD)

        now_utc      = datetime.now(timezone.utc)
        midnight_utc = datetime(now_utc.year, now_utc.month, now_utc.day,
                                tzinfo=timezone.utc)
        midnight_ts  = midnight_utc.timestamp()

        try:
            conn  = sqlite3.connect(str(_SHARED_DB), timeout=10)
            row   = conn.execute(
                "SELECT COUNT(*) FROM api_calls WHERE model_id = ? AND ts >= ?",
                (model_id, midnight_ts),
            ).fetchone()
            conn.close()
            count = row[0] if row else 0
        except Exception as exc:
            logger.warning(f"[AIChoreographer] check_daily_quota error: {exc}")
            return "ok"

        pct = count / rpd if rpd > 0 else 0.0
        if pct >= _QUOTA_BLOCK_PCT:
            logger.warning(
                f"[AIChoreographer] {model_id} daily quota at {pct*100:.0f}% "
                f"({count}/{rpd}) — BLOCKING"
            )
            return "block"
        if pct >= _QUOTA_WARN_PCT:
            logger.info(
                f"[AIChoreographer] {model_id} daily quota at {pct*100:.0f}% "
                f"({count}/{rpd}) — warning"
            )
            return "warn"
        return "ok"

    # ── Observability ─────────────────────────────────────────────────────────

    @classmethod
    def get_usage_summary(cls, hours: float = 24.0) -> Dict:
        """
        Return per-model, per-system call counts for the last N hours.
        Format: {model_id: {system_name: count, ...}, ...}
        """
        cls._ensure_db()
        if not cls._db_initialized:
            return {}

        since_ts = time.time() - (hours * 3600)
        try:
            conn = sqlite3.connect(str(_SHARED_DB), timeout=10)
            rows = conn.execute(
                """SELECT model_id, system, COUNT(*) AS cnt
                   FROM api_calls
                   WHERE ts >= ?
                   GROUP BY model_id, system
                   ORDER BY model_id, system""",
                (since_ts,),
            ).fetchall()
            conn.close()
        except Exception as exc:
            logger.warning(f"[AIChoreographer] get_usage_summary error: {exc}")
            return {}

        summary: Dict = {}
        for model_id, system, cnt in rows:
            if model_id not in summary:
                summary[model_id] = {}
            summary[model_id][system] = cnt
        return summary


# ── Module-level convenience (mirrors QuotaTracker API surface) ───────────────

def pace_and_record(model_id: str, system_name: str = "hightrade") -> None:
    AIChoreographer.pace_and_record(model_id, system_name)


def check_daily_quota(model_id: str) -> str:
    return AIChoreographer.check_daily_quota(model_id)


# ── CLI smoke-test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")

    system = sys.argv[1] if len(sys.argv) > 1 else "test"
    model  = "gemini-3.1-pro-preview"

    print(f"\nAIChoreographer smoke-test  (system={system}, model={model})")
    print("Making 3 consecutive calls — should see ~2.4s gaps between them\n")

    for i in range(3):
        t0 = time.time()
        AIChoreographer.pace_and_record(model, system)
        elapsed = time.time() - t0
        print(f"  Call {i+1} cleared in {elapsed:.3f}s")

    print(f"\nUsage summary (last 1h):\n{AIChoreographer.get_usage_summary(1)}")
    print(f"\nDaily quota status: {AIChoreographer.check_daily_quota(model)}")
