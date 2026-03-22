import sqlite3
import time
import os
import logging
from typing import Optional

log = logging.getLogger(__name__)


def get_sqlite_conn(db_path: str, retries: int = 6, backoff: float = 0.5, timeout: int = 5, check_writable: bool = True) -> sqlite3.Connection:
    """
    Try to open an sqlite3 connection with retries and exponential backoff.

    Parameters:
    - db_path: path to sqlite DB file
    - retries: number of attempts
    - backoff: base backoff in seconds (exponential)
    - timeout: sqlite timeout (seconds)
    - check_writable: if True, verify parent directory is writable before connecting

    Returns sqlite3.Connection on success or raises the last exception on failure.
    """
    last_exc: Optional[Exception] = None
    db_path = str(db_path)
    parent = os.path.dirname(db_path) or '.'

    for attempt in range(1, retries + 1):
        try:
            if check_writable:
                if not os.path.isdir(parent):
                    raise FileNotFoundError(f"DB parent directory does not exist: {parent}")
                if not os.access(parent, os.W_OK):
                    raise PermissionError(f"DB parent not writable: {parent}")
            conn = sqlite3.connect(db_path, timeout=timeout, check_same_thread=False)
            log.debug("Opened sqlite DB %s (attempt %d/%d)", db_path, attempt, retries)
            return conn
        except Exception as e:
            last_exc = e
            log.warning("SQLite connect attempt %d/%d failed for %s: %s", attempt, retries, db_path, e)
            if attempt < retries:
                sleep = backoff * (2 ** (attempt - 1))
                time.sleep(sleep)

    log.error("Failed to open sqlite DB %s after %d attempts: %s", db_path, retries, last_exc)
    raise last_exc


def quick_readonly_check(conn: sqlite3.Connection) -> str:
    """Run a quick readonly pragma check; returns the single-row result or raises.
    Caller is responsible for closing the connection if desired."""
    cur = conn.cursor()
    cur.execute("PRAGMA quick_check;")
    return cur.fetchone()[0] if cur.fetchone() else 'unknown'
