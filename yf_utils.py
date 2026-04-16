"""
yf_utils.py — yfinance helper utilities.

Provides a context manager that closes the yfinance peewee tz-cache DB
after each use, preventing FD leaks from accumulated SqliteDatabase handles.
"""
from contextlib import contextmanager


def _close_yf_cache():
    """Close the yfinance cache DBs if open. Silent on any error."""
    try:
        from yfinance.cache import _TzDBManager, _CookieDBManager, _ISINDBManager
        for manager in [_TzDBManager, _CookieDBManager, _ISINDBManager]:
            db = manager.get_database()
            if db and not db.is_closed():
                db.close()
    except Exception:
        pass


@contextmanager
def yf_session():
    """
    Context manager for yfinance calls.

    Usage:
        with yf_session():
            hist = yf.Ticker('AAPL').history(period='1d')

    Ensures the yfinance peewee tz-cache DB is closed after each use,
    preventing FD leaks under long-running processes.
    """
    try:
        yield
    finally:
        _close_yf_cache()
