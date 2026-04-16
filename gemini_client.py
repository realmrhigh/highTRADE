#!/usr/bin/env python3
"""
gemini_client.py — Unified Gemini call interface for HighTrade

Architecture (clean separation of concerns):
  ┌──────────────────────────────────────────────────────────────────────┐
  │ 1. CONFIG           Models, limits, fallback chains                 │
  │ 2. QuotaTracker     Single class: DB ops, RPM pacing, daily quota   │
  │ 3. Auth backends    _call_via_cli(), _call_via_api() — pure I/O     │
  │ 4. call()           Top-level entry: auth selection + fallback walk  │
  │ 5. Public helpers   market_context_block(), get_reset_aligned_usage()│
  └──────────────────────────────────────────────────────────────────────┘

Auth priority:
  1. Gemini CLI (OAuth via Google account) — free tier
  2. REST API fallback (API key from .env) — separate quota pool

All callers use call() — auth selection, model fallback, RPM pacing, and
quota tracking are fully automatic and transparent.

Usage:
    from gemini_client import call

    text, in_tok, out_tok = call(
        model_key='reasoning',   # 'fast' | 'balanced' | 'reasoning'
        prompt='...',
        caller='analyst',
    )
"""

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import requests
from trading_db import get_sqlite_conn
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

logger = logging.getLogger(__name__)

# Which system is running this client (hightrade | highcrypto).
# Set via HIGHTRADE_SYSTEM env var in each project's .env file.
SYSTEM_NAME: str = os.environ.get("HIGHTRADE_SYSTEM", "hightrade")

# Cross-process rate limiter — coordinates calls across simultaneously-running
# highTRADE and highCRYPTO instances sharing the same Gemini API key.
try:
    from ai_choreographer import AIChoreographer as _Choreographer
    _CHOREOGRAPHER_OK = True
except ImportError:
    _CHOREOGRAPHER_OK = False
    logger.warning("[gemini_client] ai_choreographer not found — falling back to local QuotaTracker pacing only")

# ═══════════════════════════════════════════════════════════════════════════════
# 1. CONFIGURATION — models, quotas, fallback chains
# ═══════════════════════════════════════════════════════════════════════════════

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


def _google_search_grounding_enabled() -> bool:
    """Return True when Gemini REST calls should include Google Search grounding."""
    val = os.environ.get("GEMINI_ENABLE_GOOGLE_SEARCH", "").strip().lower()
    return val in {"1", "true", "yes", "on"}

def _get_api_key() -> str:
    """Re-read GEMINI_API_KEY from .env on every call (hot-reload without restart)."""
    try:
        from dotenv import load_dotenv as _ld
        _ld(Path(__file__).parent / ".env", override=True)
    except ImportError:
        env_path = Path(__file__).parent / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith('GEMINI_API_KEY=') and not line.startswith('#'):
                    val = line.split('=', 1)[1].strip()
                    if val:
                        os.environ['GEMINI_API_KEY'] = val
    return os.environ.get("GEMINI_API_KEY", "")


# ── Model registry: every model the system can use ────────────────────────────
# Each model has exactly one set of parameters and limits.
MODELS = {
    'gemini-3.1-pro-preview': {
        'rpm': 25,   'rpd':  250,  'tpm': 1_000_000,
        'thinking_budget': -1,
        'max_output_tokens': 16384,
        'temperature': 1.0,
        'auth': 'cli',
        'label': '3.1 Pro Preview',
        'role': 'reasoning',
    },
    'gemini-3-flash-preview': {
        'rpm': 120,  'rpd': 1500,  'tpm': 1_000_000,
        'thinking_budget': 8000,
        'max_output_tokens': 8192,
        'temperature': 0.4,
        'auth': 'cli',
        'label': '3 Flash',
        'role': 'fast',
    },
    'gemini-2.5-pro': {
        'rpm': 120,  'rpd': 1500,  'tpm': 2_000_000,
        'thinking_budget': 8000,
        'max_output_tokens': 16384,
        'temperature': 1.0,
        'auth': 'cli',
        'label': '2.5 Pro',
        'role': 'fallback-cli',
    },
    'gemini-3.1-flash-lite-preview': {
        'rpm': 120,  'rpd': 1500,  'tpm': 4_000_000,
        'thinking_budget': 0,
        'max_output_tokens': 8192,
        'temperature': 0.4,
        'auth': 'rest',
        'label': '3.1 Flash Lite',
        'role': 'fallback-rest',
    },
}

# ── Model tiers — what model_key maps to ──────────────────────────────────────
MODEL_TIERS = {
    'fast':      'gemini-3-flash-preview',
    'balanced':  'gemini-3-flash-preview',
    'reasoning': 'gemini-3.1-pro-preview',
    'flash':     'gemini-3-flash-preview',     # legacy alias
    'pro':       'gemini-3.1-pro-preview',     # legacy alias
}

# ── Fallback chains — walked in order when a model fails ──────────────────────
# Each chain is a list of (model_id, auth_method) tuples.
# The system walks the chain until one succeeds.
FALLBACK_CHAINS = {
    # Reasoning: pro → flash (cli) → flash-lite (rest) → 2.5-pro (rest)
    'gemini-3.1-pro-preview': [
        ('gemini-3-flash-preview',       'cli'),
        ('gemini-3.1-flash-lite-preview', 'rest'),
        ('gemini-2.5-pro',               'rest'),
    ],
    # Fast/balanced: flash → 2.5-pro (cli) → flash-lite (rest) → 2.5-pro (rest)
    'gemini-3-flash-preview': [
        ('gemini-2.5-pro',               'cli'),
        ('gemini-3.1-flash-lite-preview', 'rest'),
        ('gemini-2.5-pro',               'rest'),
    ],
    # CLI fallbacks that themselves fail:
    'gemini-2.5-pro': [
        ('gemini-3.1-flash-lite-preview', 'rest'),
    ],
    # REST fallback
    'gemini-3.1-flash-lite-preview': [
        ('gemini-2.5-pro',               'rest'),
    ],
}

# ── Tier config overrides applied when using a model_key ──────────────────────
TIER_OVERRIDES = {
    'fast':      {'temperature': 0.4, 'thinking_budget': 8000, 'max_output_tokens': 8192},
    'balanced':  {'temperature': 1.0, 'thinking_budget': 8000, 'max_output_tokens': 8192},
    'reasoning': {'temperature': 1.0, 'thinking_budget': -1,   'max_output_tokens': 16384},
    'flash':     {'temperature': 0.4, 'thinking_budget': 8000, 'max_output_tokens': 8192},
    'pro':       {'temperature': 1.0, 'thinking_budget': -1,   'max_output_tokens': 16384},
}

# ── Quota thresholds ──────────────────────────────────────────────────────────
QUOTA_WARN_PCT  = 0.75
QUOTA_BLOCK_PCT = 0.90

# ── Backward-compat aliases (used by dashboard.py, acquisition_analyst.py) ───
MODEL_CONFIG = {}
for _key, _mid in MODEL_TIERS.items():
    _m = MODELS[_mid]
    MODEL_CONFIG[_key] = {
        'model_id': _mid,
        'thinking_budget': _m['thinking_budget'],
        'max_output_tokens': _m['max_output_tokens'],
        'temperature': _m['temperature'],
    }

QUOTA_DAILY_LIMITS = {mid: m['rpd'] for mid, m in MODELS.items()}
QUOTA_RPM_LIMITS   = {mid: m['rpm'] for mid, m in MODELS.items()}
QUOTA_SOFT_LIMITS  = QUOTA_DAILY_LIMITS   # backward compat


# ═══════════════════════════════════════════════════════════════════════════════
# 2. QUOTA TRACKER — single class for all DB ops, RPM pacing, daily quota
# ═══════════════════════════════════════════════════════════════════════════════

class QuotaTracker:
    """
    Thread-safe quota tracker backed by SQLite.

    Responsibilities:
      - Log every call with model_id, auth_method, caller, tokens, downgraded flag
      - RPM pacing: sleep the minimum interval before each call (one place)
      - Daily quota check: returns ok/warn/block
      - Rolling usage queries for dashboard display
      - Eliminates race conditions via a threading lock + DB transactions
    """

    _DB_PATH = Path(__file__).parent / 'trading_data' / 'trading_history.db'
    _lock = threading.Lock()
    _schema_ok = False

    @classmethod
    def _conn(cls) -> sqlite3.Connection:
        conn = get_sqlite_conn(str(cls._DB_PATH), timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    @classmethod
    def _ensure_schema(cls, conn: sqlite3.Connection):
        if cls._schema_ok:
            return
        conn.execute("""
            CREATE TABLE IF NOT EXISTS gemini_call_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                model_id     TEXT NOT NULL,
                model_key    TEXT NOT NULL,
                caller       TEXT DEFAULT 'unknown',
                auth_method  TEXT DEFAULT 'unknown',
                tokens_in    INTEGER DEFAULT 0,
                tokens_out   INTEGER DEFAULT 0,
                downgraded   INTEGER DEFAULT 0,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_gcl_model_time ON gemini_call_log(model_id, created_at)")
        # Add auth_method column if migrating from old schema
        try:
            conn.execute("SELECT auth_method FROM gemini_call_log LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE gemini_call_log ADD COLUMN auth_method TEXT DEFAULT 'unknown'")
        conn.commit()
        cls._schema_ok = True

    # ── Logging ────────────────────────────────────────────────────────────

    @classmethod
    def log_call(cls, model_id: str, model_key: str, caller: str,
                 auth_method: str, tokens_in: int, tokens_out: int,
                 downgraded: bool = False):
        """Write one row to gemini_call_log. Silent on any error."""
        try:
            with cls._lock:
                conn = cls._conn()
                try:
                    cls._ensure_schema(conn)
                    conn.execute(
                        "INSERT INTO gemini_call_log "
                        "(model_id, model_key, caller, auth_method, tokens_in, tokens_out, downgraded) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (model_id, model_key, caller, auth_method, tokens_in, tokens_out, int(downgraded))
                    )
                    conn.commit()
                finally:
                    conn.close()
        except Exception:
            pass  # logging must never crash a caller

    # ── RPM pacing (single enforcement point) ──────────────────────────────

    @classmethod
    def pace_for_rpm(cls, model_id: str):
        """
        Sleep just long enough to respect RPM limits. Called once before each
        API/CLI call. This is the ONLY place RPM is enforced.

        Intervals (60/RPM):
            3.1-pro-preview        ->  2.4s  (25 RPM)
            2.5-pro                ->  0.5s  (120 RPM)
            3-flash-preview        ->  0.5s  (120 RPM)
            3.1-flash-lite-preview ->  0.5s  (120 RPM)
        """
        model = MODELS.get(model_id)
        if not model:
            return
        rpm = model['rpm']
        min_interval = 60.0 / rpm

        try:
            with cls._lock:
                conn = cls._conn()
                try:
                    cls._ensure_schema(conn)
                    row = conn.execute(
                        "SELECT created_at FROM gemini_call_log "
                        "WHERE model_id = ? AND tokens_in >= 0 "
                        "ORDER BY created_at DESC LIMIT 1",
                        (model_id,)
                    ).fetchone()
                finally:
                    conn.close()

            if not row:
                return

            last_call = datetime.strptime(row[0][:19], '%Y-%m-%d %H:%M:%S')
            elapsed = (datetime.utcnow() - last_call).total_seconds()
            wait = min_interval - elapsed

            if wait > 0:
                logger.info(
                    f"RPM pacing [{model_id}]: "
                    f"last call {elapsed:.1f}s ago, need {min_interval:.0f}s gap -- "
                    f"sleeping {wait:.1f}s"
                )
                time.sleep(wait)
        except Exception:
            pass  # pacing must never crash a caller

    # ── Daily quota check ──────────────────────────────────────────────────

    @classmethod
    def check_daily_quota(cls, model_id: str) -> str:
        """
        Check calls since last Google reset against daily limit.
        Returns: 'ok' | 'warn' | 'block'
        Does NOT sleep or retry — call() handles fallback.
        """
        model = MODELS.get(model_id)
        if not model:
            return 'ok'
        daily_limit = model['rpd']
        if not daily_limit:
            return 'ok'

        try:
            since = cls._last_reset_utc(model_id).strftime('%Y-%m-%d %H:%M:%S')
            conn = cls._conn()
            try:
                cls._ensure_schema(conn)
                row = conn.execute(
                    "SELECT COUNT(*) AS calls FROM gemini_call_log "
                    "WHERE model_id = ? AND created_at >= ? AND tokens_in >= 0",
                    (model_id, since)
                ).fetchone()
            finally:
                conn.close()
            calls = row['calls'] if row else 0
            ratio = calls / daily_limit
            if ratio >= QUOTA_BLOCK_PCT:
                return 'block'
            if ratio >= QUOTA_WARN_PCT:
                return 'warn'
            return 'ok'
        except Exception:
            return 'ok'

    # ── RPM burst check ────────────────────────────────────────────────────

    @classmethod
    def check_rpm(cls, model_id: str) -> bool:
        """Return True if model is currently at/over RPM limit (last 60s)."""
        model = MODELS.get(model_id)
        if not model:
            return False
        rpm = model['rpm']
        try:
            conn = cls._conn()
            try:
                cls._ensure_schema(conn)
                row = conn.execute(
                    "SELECT COUNT(*) AS calls FROM gemini_call_log "
                    "WHERE model_id = ? AND created_at >= datetime('now', '-60 seconds') "
                    "AND tokens_in >= 0",
                    (model_id,)
                ).fetchone()
            finally:
                conn.close()
            return (row['calls'] if row else 0) >= rpm
        except Exception:
            return False

    # ── Reset time helpers ─────────────────────────────────────────────────

    @staticmethod
    def _last_reset_utc(model_id: str) -> datetime:
        """Most recent midnight UTC — quota tallies reset at 00:00 UTC daily."""
        now = datetime.utcnow()
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    @staticmethod
    def _next_reset_utc(model_id: str) -> datetime:
        return QuotaTracker._last_reset_utc(model_id) + timedelta(hours=24)

    # ── Rolling usage queries (for dashboard) ──────────────────────────────

    @classmethod
    def get_rolling_usage(cls, hours: int = 24) -> dict:
        """Call counts and token totals per model_id for the last N hours."""
        try:
            conn = cls._conn()
            try:
                cls._ensure_schema(conn)
                rows = conn.execute("""
                    SELECT model_id,
                           COUNT(*)        AS calls,
                           SUM(tokens_in)  AS tokens_in,
                           SUM(tokens_out) AS tokens_out,
                           MIN(created_at) AS oldest_call_at
                    FROM gemini_call_log
                    WHERE created_at >= datetime('now', ?)
                      AND tokens_in >= 0
                    GROUP BY model_id
                """, (f'-{hours} hours',)).fetchall()
            finally:
                conn.close()
            return {r['model_id']: dict(r) for r in rows}
        except Exception:
            return {}

    @classmethod
    def get_rolling_usage_seconds(cls, seconds: int = 60) -> dict:
        """Call counts per model_id for the last N seconds."""
        try:
            conn = cls._conn()
            try:
                cls._ensure_schema(conn)
                rows = conn.execute("""
                    SELECT model_id, COUNT(*) AS calls
                    FROM gemini_call_log
                    WHERE created_at >= datetime('now', ?)
                      AND tokens_in >= 0
                    GROUP BY model_id
                """, (f'-{seconds} seconds',)).fetchall()
            finally:
                conn.close()
            return {r['model_id']: {'calls': r['calls']} for r in rows}
        except Exception:
            return {}

    @classmethod
    def get_reset_aligned_usage(cls) -> dict:
        """
        Call counts per model_id since Google's last actual quota reset.
        Accurate counts matching the Gemini CLI. Includes auth_method breakdown.
        """
        result = {}
        try:
            conn = cls._conn()
            try:
              cls._ensure_schema(conn)
              for model_id, model in MODELS.items():
                daily_limit = model['rpd']
                last_reset  = cls._last_reset_utc(model_id)
                next_reset  = cls._next_reset_utc(model_id)
                resets_in_s = max(0, int((next_reset - datetime.utcnow()).total_seconds()))
                since_str   = last_reset.strftime('%Y-%m-%d %H:%M:%S')

                row = conn.execute("""
                    SELECT COUNT(*)        AS calls,
                           SUM(tokens_in)  AS tokens_in,
                           SUM(tokens_out) AS tokens_out
                    FROM gemini_call_log
                    WHERE model_id = ? AND created_at >= ?
                      AND tokens_in >= 0
                """, (model_id, since_str)).fetchone()

                calls   = row['calls']      or 0
                tok_in  = row['tokens_in']   or 0
                tok_out = row['tokens_out']  or 0
                pct     = calls / daily_limit if daily_limit else 0.0

                # Auth method breakdown
                auth_rows = conn.execute("""
                    SELECT COALESCE(auth_method, 'unknown') AS auth, COUNT(*) AS cnt
                    FROM gemini_call_log
                    WHERE model_id = ? AND created_at >= ?
                      AND tokens_in >= 0
                    GROUP BY auth
                """, (model_id, since_str)).fetchall()
                auth_breakdown = {r['auth']: r['cnt'] for r in auth_rows}

                result[model_id] = {
                    'calls':       calls,
                    'tokens_in':   tok_in,
                    'tokens_out':  tok_out,
                    'daily_limit': daily_limit,
                    'rpm_limit':   model['rpm'],
                    'pct':         pct,
                    'resets_in_s': resets_in_s,
                    'last_reset':  since_str,
                    'label':       model['label'],
                    'role':        model['role'],
                    'auth_breakdown': auth_breakdown,
                }
            finally:
                conn.close()
        except Exception:
            pass
        return result

    @classmethod
    def get_auth_summary(cls, hours: int = 24) -> dict:
        """Return {auth_method: call_count} for the last N hours."""
        try:
            conn = cls._conn()
            try:
                cls._ensure_schema(conn)
                rows = conn.execute("""
                    SELECT COALESCE(auth_method, 'unknown') AS auth, COUNT(*) AS cnt
                    FROM gemini_call_log
                    WHERE created_at >= datetime('now', ?)
                      AND tokens_in >= 0
                    GROUP BY auth
                """, (f'-{hours} hours',)).fetchall()
            finally:
                conn.close()
            return {r['auth']: r['cnt'] for r in rows}
        except Exception:
            return {}


# ── Public aliases (backward compat for dashboard.py and other importers) ─────
_DB_PATH = QuotaTracker._DB_PATH

def get_rolling_usage(hours: int = 24) -> dict:
    return QuotaTracker.get_rolling_usage(hours)

def get_rolling_usage_seconds(seconds: int = 60) -> dict:
    return QuotaTracker.get_rolling_usage_seconds(seconds)

def get_reset_aligned_usage() -> dict:
    return QuotaTracker.get_reset_aligned_usage()

def check_quota(model_key: str) -> str:
    """Backward-compat: check quota for a model_key. Returns 'ok'|'warn'|'block'."""
    model_id = MODEL_TIERS.get(model_key, '')
    if not model_id:
        return 'ok'
    return QuotaTracker.check_daily_quota(model_id)

# Backward-compat reset-time helpers — all models now reset at midnight UTC
QUOTA_RESET_UTC = {mid: (0, 0) for mid in MODELS}

def _last_reset_utc(model_id: str) -> datetime:
    return QuotaTracker._last_reset_utc(model_id)

def _next_reset_utc(model_id: str) -> datetime:
    return QuotaTracker._next_reset_utc(model_id)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. CLI STATUS — OAuth availability check (cached)
# ═══════════════════════════════════════════════════════════════════════════════

_cli_path: Optional[str] = None
_cli_authenticated: Optional[bool] = None
_cli_auth_retry_after: Optional[datetime] = None
_last_cli_error: str = ''

def _get_cli_status() -> Tuple[bool, str]:
    """
    Returns (available, reason).
    CLI is usable if 'gemini' is on PATH and OAuth creds exist.
    Auto-retries after 10 minutes if OAuth previously failed.
    """
    global _cli_path, _cli_authenticated, _cli_auth_retry_after

    # Auto-recover: retry CLI auth after timer expires
    if _cli_authenticated is False and _cli_auth_retry_after is not None:
        if datetime.now() >= _cli_auth_retry_after:
            logger.info("OAuth retry window elapsed -- re-checking CLI auth...")
            _cli_authenticated = None
            _cli_auth_retry_after = None

    if _cli_authenticated is not None:
        return _cli_authenticated, _cli_path or ''

    binary = shutil.which('gemini')
    if not binary:
        _cli_authenticated = False
        logger.debug("Gemini CLI not found on PATH -- using REST API")
        return False, 'CLI not installed'

    # Support both old and new CLI auth formats
    gemini_dir = Path.home() / '.gemini'
    creds_old  = gemini_dir / 'oauth_creds.json'
    creds_new  = gemini_dir / 'mcp-oauth-tokens-v2.json'

    if creds_new.exists():
        pass  # new format -- presence is sufficient
    elif creds_old.exists():
        try:
            creds = json.loads(creds_old.read_text())
            if not creds.get('refresh_token'):
                _cli_authenticated = False
                return False, 'No refresh token'
        except Exception:
            _cli_authenticated = False
            return False, 'Creds unreadable'
    else:
        _cli_authenticated = False
        logger.debug("No OAuth creds found -- using REST API")
        return False, 'Not authenticated'

    _cli_path = binary
    _cli_authenticated = True
    logger.info(f"Using Gemini CLI binary: {binary}")
    return True, binary


def _handle_cli_auth_failure():
    """Disable CLI for 10 minutes on OAuth failures (not quota errors)."""
    global _cli_authenticated, _cli_auth_retry_after
    _cli_authenticated = False
    _cli_auth_retry_after = datetime.now() + timedelta(minutes=10)
    logger.warning(
        "OAuth failure detected -- switching to REST API. "
        "Will retry CLI auth in 10 minutes."
    )


def _is_quota_error(error_text: str) -> bool:
    """Detect Google quota/rate-limit errors in CLI stderr."""
    return any(pat in error_text for pat in (
        'TerminalQuotaError', 'exhausted your capacity',
        'No capacity available', '"code": 429', "'code': 429",
    ))


def _is_auth_error(error_text: str) -> bool:
    """Detect genuine OAuth/auth failures (NOT quota errors)."""
    return any(pat in error_text for pat in (
        'UNAUTHENTICATED', 'unauthenticated',
        'invalid_grant', '401 Unauthorized',
        'not authenticated', 'No refresh token',
        'oauth_token invalid', 'Token has been expired',
    ))


# ═══════════════════════════════════════════════════════════════════════════════
# 4. AUTH BACKENDS — pure I/O, no fallback logic
# ═══════════════════════════════════════════════════════════════════════════════

def _build_thinking_config(model_id: str, thinking_budget: int) -> dict:
    """Build the correct thinkingConfig dict for the model.

    Gemini 3.x -> thinkingLevel ('minimal'|'low'|'medium'|'high')
    Gemini 2.x -> thinkingBudget (integer, -1 = dynamic)
    Lite models -> no thinking config
    """
    if 'lite' in model_id:
        return {}
    if model_id.startswith('gemini-3'):
        level = 'minimal' if thinking_budget == 0 else 'high'
        return {'thinkingLevel': level}
    else:
        if thinking_budget != 0:
            return {'thinkingBudget': thinking_budget}
        return {}


def _call_via_cli(prompt: str, model_id: str, temperature: float,
                  thinking_budget: int, max_output_tokens: int) -> Tuple[Optional[str], int, int]:
    """Call via 'gemini' CLI (OAuth). Returns (text, in_tok, out_tok).

    Hardened: if the CLI returns a non-zero exit code but stdout contains valid
    JSON with a response, we still use it. Some CLI versions return rc=1 while
    writing "Loaded cached credentials." to stderr, even on successful calls.
    """
    global _last_cli_error
    try:
        cmd = [
            _cli_path or 'gemini',
            '-p', prompt,
            '--model', model_id,
            '--output-format', 'json',
        ]
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=90,
            stdin=subprocess.DEVNULL,
            env={**os.environ, 'GEMINI_API_KEY': ''},  # force OAuth
        )

        stderr_text = (result.stderr or '').strip()

        # ── Non-zero exit handling ────────────────────────────────────────
        # The Gemini CLI sometimes exits 1 with benign stderr like
        # "Loaded cached credentials." while stdout contains valid JSON.
        # Only treat as hard failure if stdout is empty or not parseable.
        if result.returncode != 0:
            _last_cli_error = stderr_text

            # If stdout has content, try parsing it before giving up
            if result.stdout and result.stdout.strip().startswith('{'):
                try:
                    data = json.loads(result.stdout)
                    text = data.get('response', '').strip()
                    if text:
                        stats       = data.get('stats', {})
                        models      = stats.get('models', {})
                        model_stats = models.get(model_id, {})
                        tok         = model_stats.get('tokens', {})
                        in_tok      = tok.get('input', tok.get('prompt', 0))
                        out_tok     = tok.get('candidates', 0)
                        logger.info(
                            f"CLI rc={result.returncode} but response valid "
                            f"({model_id}) | in={in_tok} out={out_tok} | "
                            f"stderr: {stderr_text[:120]}"
                        )
                        return text, in_tok, out_tok
                except (json.JSONDecodeError, KeyError):
                    pass  # fall through to failure path

            logger.warning(
                f"CLI exited {result.returncode} | stderr: {stderr_text[:200]} | "
                f"stdout: {(result.stdout or '')[:200]}"
            )
            return None, 0, 0

        # ── Normal exit (rc=0) ────────────────────────────────────────────
        data = json.loads(result.stdout)
        text = data.get('response', '').strip()

        stats       = data.get('stats', {})
        models      = stats.get('models', {})
        model_stats = models.get(model_id, {})
        tok         = model_stats.get('tokens', {})
        in_tok      = tok.get('input', tok.get('prompt', 0))
        out_tok     = tok.get('candidates', 0)

        if not text:
            logger.warning("CLI returned empty response")
            return None, 0, 0

        logger.debug(f"CLI OK {model_id} | in={in_tok} out={out_tok}")
        return text, in_tok, out_tok

    except subprocess.TimeoutExpired:
        logger.error("CLI call timed out after 90s")
        return None, 0, 0
    except json.JSONDecodeError as e:
        logger.error(f"CLI JSON parse error: {e}")
        return None, 0, 0
    except Exception as e:
        logger.error(f"CLI call error: {e}")
        return None, 0, 0


def _call_via_api(prompt: str, model_id: str, temperature: float,
                  thinking_budget: int, max_output_tokens: int) -> Tuple[Optional[str], int, int]:
    """Call via REST API (API key). Returns (text, in_tok, out_tok)."""
    api_key = _get_api_key()
    if not api_key:
        logger.debug("REST API skipped -- no GEMINI_API_KEY set")
        return None, 0, 0

    url = f"{GEMINI_API_BASE}/{model_id}:generateContent?key={api_key}"

    gen_config: dict = {
        'temperature':     temperature,
        'maxOutputTokens': max_output_tokens,
    }
    thinking_cfg = _build_thinking_config(model_id, thinking_budget)
    if thinking_cfg:
        gen_config['thinkingConfig'] = thinking_cfg

    payload = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': gen_config,
    }
    if _google_search_grounding_enabled():
        payload['tools'] = [{'google_search': {}}]

    resp = None  # ensure resp is always defined for the finally block
    try:
        resp = requests.post(url, json=payload, timeout=180)
        if not resp.ok:
            try:
                err_body   = resp.json()
                err_msg    = err_body.get('error', {}).get('message', resp.text[:300])
                err_code   = err_body.get('error', {}).get('code', resp.status_code)
                err_status = err_body.get('error', {}).get('status', '')
            except Exception:
                err_msg, err_code, err_status = resp.text[:300], resp.status_code, ''
            logger.error(f"REST HTTP {resp.status_code} ({model_id}): [{err_code}] {err_status} -- {err_msg}")
            resp.raise_for_status()

        data = resp.json()
        cand   = data.get('candidates', [{}])[0]
        parts  = cand.get('content', {}).get('parts', [])
        output = [p for p in parts if 'text' in p and not p.get('thought', False)]
        text   = ''.join(p['text'] for p in output).strip()

        usage   = data.get('usageMetadata', {})
        in_tok  = usage.get('promptTokenCount', 0)
        out_tok = usage.get('candidatesTokenCount', 0)
        tht_tok = usage.get('thoughtsTokenCount', 0)
        grounding_meta = cand.get('groundingMetadata', {}) or data.get('groundingMetadata', {}) or {}
        search_queries = grounding_meta.get('webSearchQueries') or grounding_meta.get('searchQueries') or []
        grounding_chunks = grounding_meta.get('groundingChunks') or []

        if not text:
            logger.warning(f"API empty output | finish={cand.get('finishReason')} | thought={tht_tok}tok")
            return None, in_tok, out_tok

        if search_queries or grounding_chunks:
            logger.info(
                f"API OK {model_id} | in={in_tok} thought={tht_tok} out={out_tok} | "
                f"grounded={len(search_queries)}q/{len(grounding_chunks)}src"
            )
        else:
            logger.info(f"API OK {model_id} | in={in_tok} thought={tht_tok} out={out_tok}")
        return text, in_tok, out_tok

    except Exception as e:
        logger.error(f"REST API call failed ({model_id}): {e}")
        return None, 0, 0
    finally:
        # Always release the socket back to the connection pool
        if resp is not None:
            resp.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 5. MAIN CALL INTERFACE — clean fallback chain walk
# ═══════════════════════════════════════════════════════════════════════════════

def call(
    prompt: str,
    model_key: str = 'fast',
    model_id: Optional[str] = None,
    temperature: Optional[float] = None,
    thinking_budget: Optional[int] = None,
    max_output_tokens: Optional[int] = None,
    caller: str = 'unknown',
) -> Tuple[Optional[str], int, int]:
    """
    Call Gemini with automatic auth selection and model fallback.

    The fallback chain:
      1. Try requested model via its preferred auth (cli or rest)
      2. On failure, walk FALLBACK_CHAINS for that model
      3. Each step: quota check -> RPM pacing -> attempt -> log result

    Returns (text, input_tokens, output_tokens). text is None on total failure.
    """
    # Resolve model_key -> model_id
    primary_model = model_id or MODEL_TIERS.get(model_key, MODEL_TIERS['fast'])
    primary_cfg   = MODELS.get(primary_model, MODELS['gemini-3-flash-preview'])

    # Merge tier overrides with any caller-supplied overrides
    tier_ov = TIER_OVERRIDES.get(model_key, {})
    eff_temperature     = temperature if temperature is not None else tier_ov.get('temperature', primary_cfg['temperature'])
    eff_thinking_budget = thinking_budget if thinking_budget is not None else tier_ov.get('thinking_budget', primary_cfg['thinking_budget'])
    eff_max_output      = max_output_tokens if max_output_tokens is not None else tier_ov.get('max_output_tokens', primary_cfg['max_output_tokens'])

    # Build the attempt list: (model_id, auth_method, is_downgrade)
    attempts = [(primary_model, primary_cfg.get('auth', 'cli'), False)]
    for fb_model, fb_auth in FALLBACK_CHAINS.get(primary_model, []):
        attempts.append((fb_model, fb_auth, True))

    cli_ok, _ = _get_cli_status()
    quota_exhausted_models = set()

    for attempt_model, attempt_auth, is_downgrade in attempts:
        # Skip CLI attempts if CLI is unavailable
        if attempt_auth == 'cli' and not cli_ok:
            logger.debug(f"Skipping {attempt_model} (cli) -- CLI unavailable")
            continue

        # Skip if we already know this model's quota is exhausted
        if attempt_model in quota_exhausted_models:
            logger.debug(f"Skipping {attempt_model} -- quota already exhausted")
            continue

        # Check daily quota before attempting (cross-process via choreographer if available)
        if _CHOREOGRAPHER_OK:
            daily_status = _Choreographer.check_daily_quota(attempt_model)
        else:
            daily_status = QuotaTracker.check_daily_quota(attempt_model)
        if daily_status == 'block':
            logger.warning(f"  {attempt_model} daily quota at {QUOTA_BLOCK_PCT*100:.0f}%+ -- skipping")
            quota_exhausted_models.add(attempt_model)
            continue

        # Resolve effective params for this attempt model
        att_temp     = eff_temperature
        att_thinking = eff_thinking_budget
        att_max_out  = eff_max_output

        # When falling back to a different model, adapt thinking params
        if is_downgrade:
            att_cfg = MODELS.get(attempt_model, primary_cfg)
            if 'lite' in attempt_model:
                att_thinking = 0
                att_max_out  = 8192
            elif attempt_model != primary_model:
                att_thinking = min(att_thinking, 8000) if att_thinking > 0 else att_thinking
                att_max_out  = min(att_max_out, att_cfg.get('max_output_tokens', 8192))

        # RPM pacing — cross-process enforcement via choreographer (falls back to local)
        if _CHOREOGRAPHER_OK:
            _Choreographer.pace_and_record(attempt_model, SYSTEM_NAME)
        else:
            QuotaTracker.pace_for_rpm(attempt_model)

        # Attempt the call
        if attempt_auth == 'cli':
            result = _call_via_cli(prompt, attempt_model, att_temp, att_thinking, att_max_out)
        else:
            result = _call_via_api(prompt, attempt_model, att_temp, att_thinking, att_max_out)

        text, in_tok, out_tok = result

        if text is not None:
            # Success -- log and return
            QuotaTracker.log_call(
                attempt_model, model_key, caller, attempt_auth,
                in_tok, out_tok, downgraded=is_downgrade
            )
            if is_downgrade:
                logger.info(f"  Fallback success: {primary_model} -> {attempt_model} ({attempt_auth})")
            return text, in_tok, out_tok

        # Failure -- diagnose and decide
        if attempt_auth == 'cli':
            if _is_auth_error(_last_cli_error):
                _handle_cli_auth_failure()
                cli_ok = False  # skip all remaining CLI attempts
            elif _is_quota_error(_last_cli_error):
                quota_exhausted_models.add(attempt_model)
                logger.warning(f"  {attempt_model} quota exhausted (CLI) -- skipping to next")
            else:
                logger.warning(f"  {attempt_model} failed ({attempt_auth}) -- trying next fallback")
        else:
            logger.warning(f"  {attempt_model} failed ({attempt_auth}) -- trying next fallback")

    # Total failure
    logger.error(f"All models exhausted for model_key={model_key}, caller={caller}")
    return None, 0, 0


# ═══════════════════════════════════════════════════════════════════════════════
# 6. MARKET CONTEXT BLOCK — injected into all AI prompts
# ═══════════════════════════════════════════════════════════════════════════════

try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    _ET_ZONE = _ZoneInfo('America/New_York')
except ImportError:
    _ET_ZONE = None

def market_context_block(vix: Optional[float] = None,
                         after_hours_price: Optional[float] = None,
                         after_hours_chg_pct: Optional[float] = None,
                         after_hours_type: Optional[str] = None,
                         gld_price: Optional[float] = None,
                         gld_flow_trend_pct: Optional[float] = None,
                         gold_spot_price: Optional[float] = None,
                         gold_30d_chg_pct: Optional[float] = None) -> str:
    """
    Formatted string describing the current market session state.
    Inject into every AI prompt so models know whether markets are open.
    """
    try:
        if _ET_ZONE:
            now = datetime.now(_ET_ZONE)
        else:
            now = datetime.utcnow()

        day_name = now.strftime('%A')
        date_str = now.strftime('%Y-%m-%d')
        time_str = now.strftime('%I:%M %p').lstrip('0')
        weekday  = now.weekday()
        hour     = now.hour
        minute   = now.minute

        _is_weekday   = weekday < 5
        _after_open   = (hour > 9) or (hour == 9 and minute >= 30)
        _before_close = hour < 16
        market_open   = _is_weekday and _after_open and _before_close

        if market_open:
            next_open_str = "currently open"
        elif _is_weekday and not _after_open:
            next_open_str = f"today ({day_name}) at 09:30 AM ET"
        elif _is_weekday and not _before_close:
            days_until = 3 if weekday == 4 else 1
            next_day = now + timedelta(days=days_until)
            next_open_str = next_day.strftime('%A %Y-%m-%d') + ' at 09:30 AM ET'
        else:
            days_until = (7 - weekday) % 7
            if days_until == 0:
                days_until = 7
            next_day = now + timedelta(days=days_until)
            next_open_str = next_day.strftime('%A %Y-%m-%d') + ' at 09:30 AM ET'

        vix_str = f"  VIX reference: {vix:.2f}\n" if vix is not None else ""

        # After-hours / gold context lines
        extra_lines = ""
        if after_hours_price is not None:
            ah_type = after_hours_type or 'extended'
            ah_chg  = f" ({after_hours_chg_pct:+.2f}%)" if after_hours_chg_pct is not None else ""
            extra_lines += f"  SPY {ah_type}: ${after_hours_price:.2f}{ah_chg}\n"
        if gld_price is not None:
            flow_str = f" | flow trend {gld_flow_trend_pct:+.1f}%" if gld_flow_trend_pct is not None else ""
            extra_lines += f"  GLD (gold ETF): ${gld_price:.2f}{flow_str}\n"
        if gold_spot_price is not None:
            chg_str = f" | 30d chg {gold_30d_chg_pct:+.2f}%" if gold_30d_chg_pct is not None else ""
            extra_lines += f"  Gold spot (GC=F): ${gold_spot_price:.2f}{chg_str}\n"

        if market_open:
            status_line  = 'OPEN -- regular US session (NYSE/NASDAQ 09:30-16:00 ET)'
            freshness    = 'Live -- prices and VIX are current'
            warning_text = ''
        else:
            if not _is_weekday:
                reason = 'WEEKEND'
            elif not _after_open:
                reason = 'PRE-MARKET'
            else:
                reason = 'AFTER HOURS'
            status_line  = f'{reason} -- US markets CLOSED'
            freshness    = 'Stale -- VIX/prices reflect the most recent session close, NOT live data'
            warning_text = (
                '  IMPORTANT: This is an outside-hours monitoring cycle.\n'
                '      Do NOT treat stale price or VIX readings as live market conditions.\n'
                '      Do NOT describe the market as actively trading, rising, or falling.\n'
                '      Do NOT suggest intraday actions. Focus on structural signals,\n'
                '      overnight news flow, and setup for the next open.\n'
            )

        block = (
            '=== MARKET SESSION CONTEXT ========================================\n'
            f'  Date/Time (ET): {day_name} {date_str} {time_str} ET\n'
            f'  Session status: {status_line}\n'
            + (f'  Next open:      {next_open_str}\n' if not market_open else '')
            + f'  Data freshness: {freshness}\n'
            + vix_str
            + extra_lines
            + warning_text
            + '===================================================================\n'
        )
        return block

    except Exception as _e:
        return f'[market_context_block error: {_e}]\n'


# ═══════════════════════════════════════════════════════════════════════════════
# 7. UTILITY
# ═══════════════════════════════════════════════════════════════════════════════

def reset_cli_cache():
    global _cli_path, _cli_authenticated, _cli_auth_retry_after
    _cli_path = None
    _cli_authenticated = None
    _cli_auth_retry_after = None

# Legacy internal function aliases for any direct importers
def _log_call(model_id, model_key, caller, tokens_in, tokens_out, downgraded=False):
    QuotaTracker.log_call(model_id, model_key, caller, 'unknown', tokens_in, tokens_out, downgraded)

def _ensure_call_log(conn):
    QuotaTracker._ensure_schema(conn)

def _throttle_for_rpm(model_id):
    QuotaTracker.pace_for_rpm(model_id)
