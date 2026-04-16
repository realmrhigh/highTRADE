"""
trading_db.py — Single source of truth for SQLite access in highTRADE.

Public API:
    db(path, *, timeout)  — context manager: opens, yields, commits/rolls back, closes
    init_db(path)         — one-time startup: integrity check, WAL repair, durable pragmas
    checkpoint_wal(path)  — WAL TRUNCATE checkpoint; call periodically from orchestrator
    get_sqlite_conn(path) — backwards-compat shim; returns raw connection, caller closes

Everything else (SafeConnection, _SQLITE_OPEN_LOCK, sqlite_conn, db_connection,
quick_readonly_check) has been deleted. Rely on WAL + busy_timeout instead of a
global open-lock; if SQLite can't open after 2 fast retries it's a real error.
"""

import os
import sqlite3
import time
import logging
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

from db_paths import DB_PATH  # noqa: re-exported so callers can do `from trading_db import DB_PATH`

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# datetime adapter
# ---------------------------------------------------------------------------
def _adapt_datetime(dt: datetime) -> str:
    return dt.isoformat()

sqlite3.register_adapter(datetime, _adapt_datetime)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
_PER_CONN_PRAGMAS = """
PRAGMA busy_timeout=15000;
PRAGMA cache_size=-8000;
PRAGMA temp_store=MEMORY;
"""

def _apply_per_conn_pragmas(conn: sqlite3.Connection) -> None:
    conn.executescript(_PER_CONN_PRAGMAS)


def _fd_count() -> str:
    try:
        return os.popen(f"lsof -p {os.getpid()} | wc -l").read().strip()
    except Exception:
        return "unknown"


def _try_wal_recovery(db_path: str) -> bool:
    """Remove stale .shm so SQLite can replay the WAL on next open."""
    shm = db_path + "-shm"
    wal = db_path + "-wal"
    if os.path.exists(shm) and os.path.exists(wal):
        try:
            os.remove(shm)
            log.warning("Removed stale SHM for WAL recovery: %s", shm)
            return True
        except OSError as e:
            log.error("Could not remove SHM: %s", e)
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@contextmanager
def db(path: Union[str, Path] = DB_PATH, *, timeout: int = 15):
    """
    THE way to access SQLite.

        with db() as conn:
            conn.execute(...)
            # auto-commits on clean exit; auto-rollback + close on exception
    """
    path = str(path)
    last_exc: Optional[Exception] = None
    conn: Optional[sqlite3.Connection] = None
    wal_recovery_tried = False

    for attempt in range(1, 3):  # 2 retries × 100 ms
        try:
            conn = sqlite3.connect(path, timeout=timeout, check_same_thread=False)
            _apply_per_conn_pragmas(conn)
            conn.execute("SELECT 1")  # fail fast on corrupt header
            break
        except (sqlite3.DatabaseError, sqlite3.OperationalError) as e:
            last_exc = e
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None
            msg = str(e).lower()
            if not wal_recovery_tried and ("unable to open" in msg or "locked" in msg):
                wal_recovery_tried = _try_wal_recovery(path)
            if attempt < 2:
                time.sleep(0.1)
        except Exception as e:
            last_exc = e
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None
            if attempt < 2:
                time.sleep(0.1)

    if conn is None:
        log.error(
            "db() failed to open %s after 2 attempts (FDs open: %s): %s",
            path, _fd_count(), last_exc,
        )
        raise last_exc  # type: ignore[misc]

    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def init_db(path: Union[str, Path] = DB_PATH) -> None:
    """
    One-time startup call: integrity check, WAL repair, durable pragmas.
    Must be called from the main thread before any workers start.
    """
    path = str(path)
    parent = os.path.dirname(path) or '.'
    if not os.path.isdir(parent):
        raise FileNotFoundError(f"DB parent directory missing: {parent}")

    # WAL recovery before first open
    _try_wal_recovery(path)

    try:
        conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    except Exception as e:
        log.error("init_db: cannot open %s: %s", path, e)
        raise

    try:
        # Durable pragmas — set once, persisted in the DB header
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.execute("PRAGMA wal_autocheckpoint=1000")

        # Integrity check
        row = conn.execute("PRAGMA integrity_check").fetchone()
        result = row[0] if row else "unknown"
        if result != "ok":
            log.error("init_db: integrity_check=%s for %s", result, path)
        else:
            log.info("init_db: DB ok (%s)", path)

        conn.commit()
    finally:
        conn.close()


def checkpoint_wal(path: Union[str, Path] = DB_PATH) -> None:
    """WAL checkpoint — call periodically to keep the WAL file small."""
    try:
        conn = sqlite3.connect(str(path), timeout=5)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        log.debug("WAL checkpoint ok: %s", path)
    except Exception as e:
        log.warning("WAL checkpoint failed for %s: %s", path, e)


# ---------------------------------------------------------------------------
# Backwards-compat shim
# ---------------------------------------------------------------------------

def get_sqlite_conn(
    db_path: Union[str, Path],
    retries: int = 2,
    backoff: float = 0.1,
    timeout: int = 15,
    check_writable: bool = True,
) -> sqlite3.Connection:
    """
    Backwards-compatible shim. Returns a raw sqlite3.Connection.
    Caller is responsible for conn.close().

    New code should use:  with db(path) as conn:
    """
    db_path = str(db_path)
    parent = os.path.dirname(db_path) or '.'
    last_exc: Optional[Exception] = None
    wal_recovery_tried = False

    for attempt in range(1, retries + 1):
        conn = None
        try:
            if check_writable:
                if not os.path.isdir(parent):
                    raise FileNotFoundError(f"DB parent directory missing: {parent}")
                if not os.access(parent, os.W_OK):
                    raise PermissionError(f"DB parent not writable: {parent}")

            conn = sqlite3.connect(db_path, timeout=timeout, check_same_thread=False)
            _apply_per_conn_pragmas(conn)
            conn.execute("SELECT 1")
            return conn
        except (sqlite3.DatabaseError, sqlite3.OperationalError) as e:
            last_exc = e
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            msg = str(e).lower()
            if not wal_recovery_tried and ("unable to open" in msg or "locked" in msg):
                wal_recovery_tried = _try_wal_recovery(db_path)
            if attempt < retries:
                time.sleep(backoff)
        except Exception as e:
            last_exc = e
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            if attempt < retries:
                time.sleep(backoff)

    log.error(
        "get_sqlite_conn: failed after %d attempts for %s (FDs: %s): %s",
        retries, db_path, _fd_count(), last_exc,
    )
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Legacy aliases (kept so old imports don't break)
# ---------------------------------------------------------------------------
db_connection = db   # `with db_connection(path) as conn:` still works
sqlite_conn = db     # `with sqlite_conn(path) as conn:` still works
