# Debug Log: SQLite "unable to open database file" — 2026-04-14

**Status: RESOLVED**

---

## Symptom

`hightrade_20260414.log` (and all prior days) filled with:

```
WARNING - SQLite connect attempt 1/6 failed for .../trading_history.db: unable to open database file
...
ERROR   - Failed to open sqlite DB .../trading_history.db after 6 attempts: unable to open database file
WARNING - 🔴 Subscription refresh DB error: unable to open database file
```

Repeating every 60 seconds. The orchestrator kept running (stream alive, DEFCON updating) but subscription refresh was broken, meaning new conditionals/positions weren't being picked up.

---

## What We Ruled Out First

| Theory | Test | Result |
|---|---|---|
| DB file missing/corrupt | `sqlite3 trading_history.db "SELECT count(*) FROM sqlite_master"` | **81 tables, OK** |
| Permissions / ACL | `ls -lae`, `xattr` | File owned by traderbot, no ACL, no quarantine |
| WAL corruption | `PRAGMA integrity_check` + `PRAGMA wal_checkpoint(TRUNCATE)` | `ok`, clean |
| Another process holding lock | `lsof trading_history.db` | **No holders at all** |
| Path resolution wrong | Python from correct cwd | Opens fine from CLI |
| Sandbox / TCC blocking | `log show` | N/A (macOS log arg parsing error) |

The DB file was perfectly healthy. The orchestrator's cwd was correct. Python could open it fine from the command line. Yet the live process could not.

---

## Root Cause

**File descriptor exhaustion.** macOS launchd default soft limit: **256 FDs**.

```
lsof -p 99261 | awk '{print $4}' | grep -E "^[0-9]" | sed 's/[urw]$//' | sort -nu | wc -l
# → 256   (maxed out, highest FD = 255)

lsof -p 99261 | grep -c "tkr-tz"
# → 214   (yfinance cache files consuming 214 of 256 FDs)
```

### Why yfinance leaked 214 FDs

`yfinance` uses `peewee.SqliteDatabase` for its timezone cache (`tkr-tz.db`). By default, peewee uses `thread_safe=True`, which means it stores the sqlite3 connection in **`threading.local`** — one connection per thread.

The orchestrator runs yfinance calls from many threads (15-min cycle workers, proximity checks, news loops, etc.). Each thread opens a new connection to `tkr-tz.db` and **never closes it** when the thread exits. Over ~107 cycles, 214 FDs accumulated until the table was full.

When the table is full, the next `sqlite3.connect()` call for **any** file returns `unable to open database file` — including `trading_history.db` which is completely unrelated.

### Why the existing patch didn't work

`hightrade_orchestrator.py` had a `_yf_close_cache()` function that called `db.close()` after yfinance calls. **But** `db.close()` on a thread-safe peewee DB only closes the **current thread's** connection. Other threads' connections in `threading.local` remain open indefinitely.

The old `_yf_patch_cache_threadsafe()` function was a **no-op** with a comment saying "thread_safe=False caused ProgrammingErrors" — so the leak was never actually stopped.

---

## Fix Applied

### 1. `hightrade_orchestrator.py` — switch peewee to shared connection

Added `_yf_patch_shared_connection()` which runs at import time (before any threads start):

```python
def _yf_patch_shared_connection():
    from yfinance.cache import _TzDBManager, _CookieDBManager, _ISINDBManager
    for manager in [_TzDBManager, _CookieDBManager, _ISINDBManager]:
        db = manager.get_database()
        if db is not None and getattr(db, 'thread_safe', False):
            db.thread_safe = False
            db._state = _peewee_mod._ConnectionState()
            # Keep db._lock — peewee's connect() uses it
```

This replaces `threading.local` (`_ConnectionLocal`) with a plain `_ConnectionState`, so **all threads share one connection**. Combined with `_yf_close_cache()` called after each yfinance operation, the FD count stays at ~1-3 instead of growing unbounded.

**Tested:** `yfinance 1.2.0 + peewee 4.0.1` — `yf.Ticker('SPY').info` works correctly after patch.

### 2. `com.hightrade3.orchestrator.plist` — bump FD limit

Added `SoftResourceLimits` and `HardResourceLimits` to raise the ceiling to 10240:

```xml
<key>SoftResourceLimits</key>
<dict>
    <key>NumberOfFiles</key>
    <integer>10240</integer>
</dict>
<key>HardResourceLimits</key>
<dict>
    <key>NumberOfFiles</key>
    <integer>10240</integer>
</dict>
```

This is a **safety net** — the primary fix is the patch above. If a different leak emerges in the future, this buys 40x more runway before hitting a wall.

---

## Verification

After restarting via `launchctl unload/load`:

```
lsof -p 48065 | grep -c "tkr-tz"   # → 7   (was 214)
lsof -p 48065 | grep "trading_history"  # → DB is open and healthy
total unique FDs: 28   (was 256/256)
```

Log output after next cycle:
```
INFO - 💾 Recording to database...
INFO - ✅ DEFCON Level: 5/5
INFO - ✅ Signal Score: 16.6/100
```

No `unable to open database file` errors.

---

## If It Comes Back

If this error returns in a future session:

1. Run `lsof -p $(pgrep -f hightrade_orchestrator) | awk '{print $4}' | grep -E "^[0-9]" | sed 's/[urw]$//' | sort -nu | wc -l` — if it's near 256 or 10240, it's another FD leak
2. Run `lsof -p $(pgrep -f hightrade_orchestrator) | grep -c "tkr-tz"` — if >10, yfinance leak is back
3. Check if a new code path calls yfinance **outside** the patched `_PatchedTicker` or `_patched_download` wrappers
4. The `thread_safe=False` patch only covers connections opened **after** the patch runs. If any module imports yfinance and calls it before `hightrade_orchestrator.py` loads, those threads won't be patched. Ensure the patch runs before any background threads start.

---

## Files Changed

- `/Users/traderbot/Documents/highTRADE/hightrade_orchestrator.py` — lines 52–130 (yfinance FD-leak guard section)
- `/Users/traderbot/Library/LaunchAgents/com.hightrade3.orchestrator.plist` — added SoftResourceLimits/HardResourceLimits
