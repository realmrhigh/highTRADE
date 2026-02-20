#!/usr/bin/env python3
"""
health_agent.py — Bi-weekly system health + meta-learning check.

Fires every other Thursday (or on demand) and does four things:

  1. API Health   — ping yfinance, FRED, Gemini CLI, Capitol Trades
  2. Signal Check — verify monitoring cycles are running, DB write recency
  3. Gap Dedup    — scan data_gaps_json across all tables, surface recurring
                   gaps (≥2 appearances in 14 days) and flag them for coding
  4. Model Scan   — check Gemini model list for IDs newer than what we're
                   running; flag for manual upgrade

Results posted to #all-hightrade via send_notify().

Schedule: run_health_check() is the main entry point.
          Returns a dict with status, summary, and flagged items.
"""

import json
import logging
import sqlite3
import subprocess
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SCRIPT_DIR  = Path(__file__).parent.resolve()
DB_PATH     = SCRIPT_DIR / 'trading_data' / 'trading_history.db'
CONFIG_PATH = SCRIPT_DIR / 'trading_data' / 'orchestrator_config.json'
STATE_PATH  = SCRIPT_DIR / 'trading_data' / 'health_state.json'

# How many times a gap must appear in the window to be flagged as recurring
GAP_RECURRENCE_THRESHOLD = 2
# Window for recurring-gap analysis (days)
GAP_WINDOW_DAYS = 14

# Gemini models we're currently running — health check compares against live list
CURRENT_MODELS = {
    'gemini-2.5-flash',
    'gemini-3-pro-preview',
}

# Known model families to watch for upgrades
TRACKED_MODEL_PREFIXES = ('gemini-2.5', 'gemini-3', 'gemini-3.1', 'gemini-2.0')


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _load_state() -> Dict:
    """Load persistent health state (last run date, previously seen gaps, etc.)."""
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_state(state: Dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str))


# ── Check 1: API Health ────────────────────────────────────────────────────────

def _check_apis(fred_api_key: Optional[str]) -> Tuple[List[str], List[str]]:
    """
    Returns (ok_list, down_list).
    Checks: yfinance, FRED, Gemini CLI, Capitol Trades.
    """
    ok, down = [], []

    # --- yfinance ---
    try:
        import yfinance as yf
        hist = yf.Ticker('SPY').history(period='1d')
        if len(hist) > 0:
            ok.append('yfinance')
        else:
            down.append('yfinance (empty response)')
    except Exception as e:
        down.append(f'yfinance ({e})')

    # --- FRED ---
    if fred_api_key:
        try:
            import requests
            r = requests.get(
                'https://api.stlouisfed.org/fred/series/observations',
                params={'series_id': 'DGS10', 'api_key': fred_api_key,
                        'limit': 1, 'sort_order': 'desc', 'file_type': 'json'},
                timeout=10
            )
            if r.status_code == 200:
                ok.append('FRED')
            else:
                down.append(f'FRED (HTTP {r.status_code})')
        except Exception as e:
            down.append(f'FRED ({e})')
    else:
        down.append('FRED (no api_key configured)')

    # --- Gemini CLI ---
    try:
        result = subprocess.run(
            ['gemini', '-p', 'ping', '--model', 'gemini-2.5-flash'],
            capture_output=True, text=True, timeout=20
        )
        if result.returncode == 0:
            ok.append('Gemini CLI')
        else:
            down.append(f'Gemini CLI (exit {result.returncode})')
    except FileNotFoundError:
        down.append('Gemini CLI (binary not found)')
    except subprocess.TimeoutExpired:
        down.append('Gemini CLI (timeout)')
    except Exception as e:
        down.append(f'Gemini CLI ({e})')

    # --- Capitol Trades ---
    try:
        import requests
        r = requests.get(
            'https://www.capitoltrades.com/trades?page=1',
            headers={'User-Agent': 'HighTrade Health Check'},
            timeout=10
        )
        if r.status_code in (200, 403):
            # 403 = anti-bot, but site is reachable
            ok.append('Capitol Trades (reachable)')
        else:
            down.append(f'Capitol Trades (HTTP {r.status_code})')
    except Exception as e:
        down.append(f'Capitol Trades ({e})')

    return ok, down


# ── Check 2: Signal Recency ────────────────────────────────────────────────────

def _check_signal_recency() -> Tuple[bool, str]:
    """
    Verify the monitoring loop is actually writing cycles.
    Returns (healthy: bool, message: str).
    Expected: at least one monitoring cycle in the last 30 minutes.
    """
    try:
        conn = _get_conn()
        row = conn.execute("""
            SELECT monitoring_date || ' ' || monitoring_time AS last_ts
            FROM signal_monitoring
            ORDER BY monitoring_date DESC, monitoring_time DESC
            LIMIT 1
        """).fetchone()
        conn.close()

        if not row or not row['last_ts']:
            return False, "No monitoring cycles found in signal_monitoring table"

        last_str = row['last_ts']
        # Handle both ISO formats
        for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
            try:
                last_dt = datetime.strptime(last_str[:26], fmt)
                break
            except ValueError:
                continue
        else:
            return True, f"Last cycle: {last_str} (parse format unknown)"

        age_minutes = (datetime.now() - last_dt).total_seconds() / 60
        if age_minutes > 30:
            return False, f"Last monitoring cycle was {age_minutes:.0f} min ago (expected ≤30)"
        return True, f"Monitoring healthy — last cycle {age_minutes:.0f}m ago"

    except Exception as e:
        return False, f"signal_monitoring query failed: {e}"


# ── Check 3: Recurring Data Gaps ──────────────────────────────────────────────

def _collect_recent_gaps(window_days: int = GAP_WINDOW_DAYS) -> Counter:
    """
    Scan data_gaps_json from daily_briefings and conditional_tracking
    for the last `window_days` days. Returns a Counter of gap strings.
    """
    cutoff = (datetime.now() - timedelta(days=window_days)).strftime('%Y-%m-%d')
    gap_counter: Counter = Counter()

    conn = _get_conn()

    # daily_briefings
    try:
        rows = conn.execute("""
            SELECT data_gaps_json FROM daily_briefings
            WHERE date >= ? AND data_gaps_json IS NOT NULL
        """, (cutoff,)).fetchall()
        for row in rows:
            try:
                gaps = json.loads(row[0])
                if isinstance(gaps, list):
                    for g in gaps:
                        if g and str(g).lower() not in ('none', ''):
                            gap_counter[g.strip().lower()] += 1
            except Exception:
                pass
    except Exception:
        pass

    # conditional_tracking
    try:
        rows = conn.execute("""
            SELECT data_gaps_json FROM conditional_tracking
            WHERE date_created >= ? AND data_gaps_json IS NOT NULL
        """, (cutoff,)).fetchall()
        for row in rows:
            try:
                gaps = json.loads(row[0])
                if isinstance(gaps, list):
                    for g in gaps:
                        if g and str(g).lower() not in ('none', ''):
                            gap_counter[g.strip().lower()] += 1
            except Exception:
                pass
    except Exception:
        pass

    conn.close()
    return gap_counter


def _identify_recurring_gaps(gap_counter: Counter, state: Dict) -> Tuple[List[str], List[str]]:
    """
    Split gaps into:
      - recurring: appeared >= GAP_RECURRENCE_THRESHOLD times (candidate for coding in)
      - new_since_last_run: gaps not seen in previous health run (informational)
    Also deduplicate against previously flagged gaps so we don't re-alert
    the same codeable item every two weeks.
    """
    previously_flagged = set(state.get('flagged_gaps', []))
    recurring, new_items = [], []

    for gap, count in gap_counter.most_common():
        if count >= GAP_RECURRENCE_THRESHOLD:
            # Only surface if not already flagged in a prior run
            if gap not in previously_flagged:
                recurring.append(f"{gap} (×{count})")
        elif gap not in previously_flagged:
            new_items.append(gap)

    return recurring, new_items


# ── Check 4: Model Update Scanner ─────────────────────────────────────────────

def _check_model_updates() -> List[str]:
    """
    Query Gemini CLI for available models and compare against CURRENT_MODELS.
    Returns list of newer model IDs we're not running yet.
    Fails silently if CLI is unavailable.
    """
    new_models = []
    try:
        result = subprocess.run(
            ['gemini', 'models', 'list'],
            capture_output=True, text=True, timeout=20
        )
        if result.returncode != 0:
            return []

        output = result.stdout + result.stderr
        # Look for lines that contain a model id matching tracked prefixes
        for line in output.splitlines():
            line = line.strip()
            for prefix in TRACKED_MODEL_PREFIXES:
                if prefix in line.lower():
                    # Extract model id — typically looks like gemini-3.1-pro-preview
                    import re
                    matches = re.findall(r'gemini-[\w.\-]+', line, re.IGNORECASE)
                    for m in matches:
                        m_clean = m.lower().strip('.,;:')
                        if m_clean not in CURRENT_MODELS:
                            new_models.append(m_clean)

        return list(dict.fromkeys(new_models))  # deduplicate, preserve order

    except Exception:
        return []


# ── Main Health Check ──────────────────────────────────────────────────────────

def run_health_check(force: bool = False) -> Dict:
    """
    Main entry point. Runs all four health checks and returns a result dict.

    Bi-weekly throttle: skips unless ≥13 days since last run, or force=True.

    Result dict keys:
      status          : 'ok' | 'warning' | 'critical'
      summary         : one-line human-readable summary
      apis_ok         : list of passing APIs
      apis_down       : list of failing APIs
      signal_healthy  : bool
      signal_message  : str
      recurring_gaps  : list of gaps flagged for coding in
      new_gaps        : list of new-this-window gaps (informational)
      new_models      : list of model IDs available but not running
      run_date        : ISO date string
    """
    state = _load_state()
    today = datetime.now().strftime('%Y-%m-%d')

    # Bi-weekly throttle
    last_run = state.get('last_run_date', '')
    if not force and last_run:
        try:
            last_dt = datetime.strptime(last_run, '%Y-%m-%d')
            days_since = (datetime.now() - last_dt).days
            if days_since < 13:
                logger.info(f"  ⏭️  Health check skipped — last ran {days_since}d ago (next in {13-days_since}d)")
                return {'status': 'skipped', 'summary': f'Last ran {days_since}d ago', 'run_date': today}
        except ValueError:
            pass

    logger.info("🏥 Running bi-weekly system health check...")

    # Load FRED key
    fred_api_key = None
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        fred_api_key = cfg.get('fred_api_key')
    except Exception:
        pass

    # ── Run all checks ────────────────────────────────────────────────────────
    apis_ok, apis_down = _check_apis(fred_api_key)
    signal_healthy, signal_msg = _check_signal_recency()
    gap_counter = _collect_recent_gaps()
    recurring_gaps, new_gaps = _identify_recurring_gaps(gap_counter, state)
    new_models = _check_model_updates()

    # ── Determine overall status ──────────────────────────────────────────────
    if apis_down and any('yfinance' in a or 'Gemini' in a for a in apis_down):
        status = 'critical'
    elif apis_down or not signal_healthy:
        status = 'warning'
    else:
        status = 'ok'

    # ── Build summary ─────────────────────────────────────────────────────────
    parts = []
    parts.append(f"{len(apis_ok)}/{len(apis_ok)+len(apis_down)} APIs healthy")
    if not signal_healthy:
        parts.append("⚠️ monitoring loop stale")
    if recurring_gaps:
        parts.append(f"{len(recurring_gaps)} recurring gaps need coding")
    if new_models:
        parts.append(f"{len(new_models)} model update(s) available")
    summary = " | ".join(parts) if parts else "All systems nominal"

    result = {
        'status':          status,
        'summary':         summary,
        'apis_ok':         apis_ok,
        'apis_down':       apis_down,
        'signal_healthy':  signal_healthy,
        'signal_message':  signal_msg,
        'recurring_gaps':  recurring_gaps,
        'new_gaps':        new_gaps,
        'new_models':      new_models,
        'run_date':        today,
        'gap_counts':      dict(gap_counter.most_common(20)),
    }

    # ── Log results ───────────────────────────────────────────────────────────
    emoji_map = {'ok': '✅', 'warning': '⚠️', 'critical': '🚨'}
    logger.info(f"  {emoji_map.get(status,'📊')} Health: {summary}")
    if apis_down:
        logger.warning(f"  🔴 APIs down: {', '.join(apis_down)}")
    logger.info(f"  📡 {signal_msg}")
    if recurring_gaps:
        logger.info(f"  🔁 Recurring gaps (flag for coding): {' | '.join(recurring_gaps)}")
    if new_models:
        logger.info(f"  🆕 New Gemini models available: {', '.join(new_models)}")

    # ── Persist state ─────────────────────────────────────────────────────────
    # Add newly flagged recurring gaps to permanent list so we don't re-alert
    all_flagged = set(state.get('flagged_gaps', []))
    for g in recurring_gaps:
        # Strip the count suffix before storing
        bare = g.split(' (×')[0].strip()
        all_flagged.add(bare)

    state['last_run_date']  = today
    state['flagged_gaps']   = sorted(all_flagged)
    state['last_result']    = result
    _save_state(state)

    return result


def _write_to_db(result: Dict) -> None:
    """
    Persist health check result to a health_checks table for historical review.
    """
    try:
        conn = _get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS health_checks (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date        TEXT NOT NULL,
                status          TEXT NOT NULL,
                summary         TEXT,
                apis_ok_json    TEXT,
                apis_down_json  TEXT,
                signal_healthy  INTEGER,
                signal_message  TEXT,
                recurring_gaps_json TEXT,
                new_gaps_json   TEXT,
                new_models_json TEXT,
                gap_counts_json TEXT,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            INSERT INTO health_checks
            (run_date, status, summary, apis_ok_json, apis_down_json,
             signal_healthy, signal_message, recurring_gaps_json,
             new_gaps_json, new_models_json, gap_counts_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            result['run_date'],
            result['status'],
            result['summary'],
            json.dumps(result.get('apis_ok', [])),
            json.dumps(result.get('apis_down', [])),
            1 if result.get('signal_healthy') else 0,
            result.get('signal_message', ''),
            json.dumps(result.get('recurring_gaps', [])),
            json.dumps(result.get('new_gaps', [])),
            json.dumps(result.get('new_models', [])),
            json.dumps(result.get('gap_counts', {})),
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"  ⚠️  Health check DB write failed: {e}")


# ── Orchestrator-facing entry point ───────────────────────────────────────────

def run_and_notify(alerts, force: bool = False) -> Optional[Dict]:
    """
    Convenience wrapper: run health check, write to DB, send to #all-hightrade.
    Returns result dict or None if skipped.
    Called by the orchestrator on bi-weekly schedule.
    """
    result = run_health_check(force=force)

    if result.get('status') == 'skipped':
        return None

    _write_to_db(result)

    alerts.send_notify('health_report', {
        'status':          result['status'],
        'summary':         result['summary'],
        'apis_down':       result['apis_down'],
        'new_models':      result['new_models'],
        'recurring_gaps':  result['recurring_gaps'],
    })

    return result


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    force = '--force' in sys.argv
    result = run_health_check(force=force)

    if result.get('status') == 'skipped':
        print(f"\n⏭️  {result['summary']}")
        sys.exit(0)

    print(f"\n{'='*60}")
    print(f"HEALTH REPORT — {result['run_date']}")
    print(f"{'='*60}")
    print(f"Status  : {result['status'].upper()}")
    print(f"Summary : {result['summary']}")
    print(f"\nAPIs OK   : {', '.join(result['apis_ok']) or 'none'}")
    print(f"APIs Down : {', '.join(result['apis_down']) or 'none'}")
    print(f"\nSignal    : {result['signal_message']}")
    print(f"\nRecurring gaps (flag for coding):")
    for g in result['recurring_gaps']:
        print(f"  🔁 {g}")
    if not result['recurring_gaps']:
        print("  none")
    print(f"\nNew models available:")
    for m in result['new_models']:
        print(f"  🆕 {m}")
    if not result['new_models']:
        print("  none")
    print(f"\nAll gaps seen (last {GAP_WINDOW_DAYS}d):")
    for g, c in sorted(result.get('gap_counts', {}).items(), key=lambda x: -x[1]):
        print(f"  {c:>2}× {g}")
