import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = SCRIPT_DIR / 'trading_data' / 'trading_history.db'


def get_db_path() -> Path:
    """Return the canonical repo-local HighTrade SQLite path."""
    return DB_PATH


def ensure_db_parent() -> Path:
    """Create the DB parent directory if needed and return the canonical path."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return DB_PATH


def verify_db_integrity(db_path: Path = None) -> bool:
    """Run a quick integrity check on the database at startup.

    Returns True if the database passes, False if corrupt or missing.
    Logs warnings on failure but does not raise.
    """
    db_path = db_path or DB_PATH
    if not db_path.exists():
        log.warning("Database does not exist: %s", db_path)
        return False
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        result = conn.execute("PRAGMA quick_check").fetchone()
        journal = conn.execute("PRAGMA journal_mode").fetchone()
        conn.close()
        ok = result and result[0] == 'ok'
        if not ok:
            log.error("DB integrity check FAILED for %s: %s", db_path, result)
        else:
            log.info("DB integrity OK (%s, journal=%s)", db_path, journal[0] if journal else '?')
        return ok
    except Exception as e:
        log.error("DB integrity check error for %s: %s", db_path, e)
        return False
