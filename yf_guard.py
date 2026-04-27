"""
yf_guard.py — yfinance FD-leak + hang guard for highTRADE.

ROOT CAUSE (diagnosed 2026-04-14):
  yfinance uses peewee SqliteDatabase with thread_safe=True, which means
  every thread gets its own sqlite3 connection to tkr-tz.db via threading.local.
  Those per-thread connections are NEVER closed when the thread exits, so after
  ~107 cycles the process hits macOS's 256-FD soft limit and "unable to open
  database file" starts appearing for ALL subsequent sqlite opens — including
  trading_history.db. The DB file itself is perfectly healthy.

FIX v2 (2026-04-15): process-wide threading.Lock to serialize yfinance cache
  access; close-after-use closes peewee handles.

FIX v3 (2026-04-15): force threads=False in patched yf.download().

FIX v4 (2026-04-18): hard timeout on every patched call. A hung socket read
  inside yfinance would previously block forever while holding _YF_CACHE_LOCK,
  starving every other yfinance caller in the process (this hung the 09:30
  morning flash briefing, which in turn blocked the monitoring loop). Now each
  patched call runs in a worker thread with future.result(timeout), lock is
  acquired with a bounded wait, and timeouts raise YFGuardTimeout so callers
  can recover. Zombie worker threads are accepted as a tradeoff — they're
  stuck on socket recv and will exit when the OS closes the connection; the
  calling thread is freed immediately and the lock is released.

Usage:
    import yf_guard
    yf_guard.install()
"""

import threading
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutTimeout

logger = logging.getLogger(__name__)

# Per-call network timeout (seconds). Most legit yfinance downloads complete
# in <5s; 45s gives plenty of headroom for slow-but-working calls while still
# bounding pathological hangs.
DEFAULT_CALL_TIMEOUT = 45.0

# How long to wait for _YF_CACHE_LOCK before giving up. Must be >= worst-case
# legitimate call duration (DEFAULT_CALL_TIMEOUT) plus queueing slack.
LOCK_ACQUIRE_TIMEOUT = 90.0

_YF_CACHE_LOCK = threading.Lock()
_installed = False

# Shared executor for timeout-bounded calls. max_workers is generous so that
# a zombie (timed-out but still hung) worker doesn't starve new calls.
_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="yf_guard")


class YFGuardTimeout(Exception):
    """Raised when a yfinance call exceeds the guard timeout or when the
    cache lock can't be acquired in time. Callers should treat this as a
    recoverable 'skip this fetch' signal, not a fatal error."""


def _yf_close_cache() -> None:
    """Close yfinance peewee cache DB handles for the current thread.
    Best-effort; tolerates version drift and closed handles."""
    try:
        from yfinance.cache import _TzDBManager, _CookieDBManager, _ISINDBManager
        for manager in [_TzDBManager, _CookieDBManager, _ISINDBManager]:
            try:
                db = manager.get_database()
                if db is not None and not db.is_closed():
                    db.close()
            except Exception:
                pass
    except Exception:
        pass


def close_cache() -> None:
    """Public helper: close yfinance cache handles under the lock.
    Uses LOCK_ACQUIRE_TIMEOUT so a stuck caller can't hang close_cache forever.
    Silently returns if the lock can't be acquired — the caller is trying to
    clean up, not block."""
    if _YF_CACHE_LOCK.acquire(timeout=LOCK_ACQUIRE_TIMEOUT):
        try:
            _yf_close_cache()
        finally:
            _YF_CACHE_LOCK.release()
    else:
        logger.warning("yf_guard.close_cache: lock busy, skipping")


def _guarded_call(fn, args, kwargs, label: str, timeout: float = DEFAULT_CALL_TIMEOUT):
    """Run `fn(*args, **kwargs)` under _YF_CACHE_LOCK with a hard timeout.
    On timeout, releases the lock and raises YFGuardTimeout; the worker
    thread may continue running (zombie) until its socket unblocks."""
    if not _YF_CACHE_LOCK.acquire(timeout=LOCK_ACQUIRE_TIMEOUT):
        logger.error(f"yf_guard: lock busy >{LOCK_ACQUIRE_TIMEOUT}s for {label} — aborting")
        raise YFGuardTimeout(f"yf_guard lock contention on {label}")
    try:
        future = _EXECUTOR.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout=timeout)
        except _FutTimeout:
            logger.error(f"yf_guard: {label} exceeded {timeout}s — abandoning (zombie thread)")
            raise YFGuardTimeout(f"{label} timed out after {timeout}s")
    finally:
        _yf_close_cache()
        _YF_CACHE_LOCK.release()


def install() -> None:
    """Patch yfinance entry points with _YF_CACHE_LOCK + timeout + close-after-use.
    Idempotent — safe to call multiple times."""
    global _installed
    if _installed:
        return

    try:
        import yfinance as _yf_mod
        import yfinance.multi as _yf_multi

        _orig_download_one = _yf_multi._download_one

        def _patched_download_one(*a, **kw):
            return _guarded_call(_orig_download_one, a, kw, "yf._download_one")

        _yf_multi._download_one = _patched_download_one

        # Patch download — force threads=False so yfinance's internal pool
        # can't spawn workers that bypass our guard (fix v3 still applies).
        _orig_download = _yf_mod.download

        def _patched_download(*a, **kw):
            kw['threads'] = False
            return _guarded_call(_orig_download, a, kw, "yf.download")

        _yf_mod.download = _patched_download

        _OrigTicker = _yf_mod.Ticker

        class _PatchedTicker(_OrigTicker):
            def history(self, *a, **kw):
                return _guarded_call(
                    lambda *aa, **kk: _OrigTicker.history(self, *aa, **kk),
                    a, kw, f"yf.Ticker({getattr(self, 'ticker', '?')}).history",
                )

            @property
            def fast_info(self):
                return _guarded_call(
                    lambda: _OrigTicker.fast_info.fget(self),
                    (), {}, f"yf.Ticker({getattr(self, 'ticker', '?')}).fast_info",
                )

        _yf_mod.Ticker = _PatchedTicker
        _installed = True

    except Exception as e:
        logger.warning(f"yf_guard.install failed: {e}")
