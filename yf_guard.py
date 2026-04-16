"""
yf_guard.py — yfinance FD-leak guard for highTRADE.

ROOT CAUSE (diagnosed 2026-04-14):
  yfinance uses peewee SqliteDatabase with thread_safe=True, which means
  every thread gets its own sqlite3 connection to tkr-tz.db via threading.local.
  Those per-thread connections are NEVER closed when the thread exits, so after
  ~107 cycles the process hits macOS's 256-FD soft limit and "unable to open
  database file" starts appearing for ALL subsequent sqlite opens — including
  trading_history.db. The DB file itself is perfectly healthy.

FIX v2 (2026-04-15): Use a process-wide threading.Lock to serialize all yfinance
  cache access. thread_safe=False caused "Connection already opened" / "SQLite
  objects created in a thread" errors when multiple threads (sector rotation,
  download workers) hit the shared connection simultaneously.
  The lock means only one thread uses the cache at a time — same FD count (1)
  without the cross-thread sqlite3 conflicts.

FIX v3 (2026-04-15): Force threads=False in patched yf.download() to prevent
  yfinance's internal _multitasking thread pool from spawning worker threads that
  call _download_one directly from new threads — bypassing _YF_CACHE_LOCK entirely.
  With threads=False, all downloads are sequential in the calling thread so the
  lock + close-after-use fully controls every sqlite cache access.

Usage:
    import yf_guard
    yf_guard.install()
"""

import threading

_YF_CACHE_LOCK = threading.Lock()
_installed = False


def _yf_close_cache() -> None:
    """Close yfinance peewee cache DB handles for the current thread.
    Must be called while holding _YF_CACHE_LOCK."""
    try:
        from yfinance.cache import _TzDBManager, _CookieDBManager, _ISINDBManager
        for manager in [_TzDBManager, _CookieDBManager, _ISINDBManager]:
            db = manager.get_database()
            if db is not None and not db.is_closed():
                db.close()
    except Exception:
        pass


def install() -> None:
    """Patch yfinance entry points with _YF_CACHE_LOCK + close-after-use.
    Idempotent — safe to call multiple times."""
    global _installed
    if _installed:
        return

    try:
        import yfinance as _yf_mod
        import yfinance.multi as _yf_multi

        # Patch _download_one
        _orig_download_one = _yf_multi._download_one

        def _patched_download_one(*a, **kw):
            with _YF_CACHE_LOCK:
                try:
                    return _orig_download_one(*a, **kw)
                finally:
                    _yf_close_cache()

        _yf_multi._download_one = _patched_download_one

        # Patch download — force threads=False to prevent internal thread pool
        # from spawning workers that bypass _YF_CACHE_LOCK (fix v3)
        _orig_download = _yf_mod.download

        def _patched_download(*a, **kw):
            kw['threads'] = False
            with _YF_CACHE_LOCK:
                try:
                    return _orig_download(*a, **kw)
                finally:
                    _yf_close_cache()

        _yf_mod.download = _patched_download

        # Patch Ticker.history and fast_info
        _OrigTicker = _yf_mod.Ticker

        class _PatchedTicker(_OrigTicker):
            def history(self, *a, **kw):
                with _YF_CACHE_LOCK:
                    try:
                        return super().history(*a, **kw)
                    finally:
                        _yf_close_cache()

            @property
            def fast_info(self):
                with _YF_CACHE_LOCK:
                    result = super().fast_info
                    _yf_close_cache()
                    return result

        _yf_mod.Ticker = _PatchedTicker
        _installed = True

    except Exception:
        pass  # yfinance not installed or import error — silently skip
