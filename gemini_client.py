#!/usr/bin/env python3
"""
gemini_client.py — Unified Gemini call interface for HighTrade

Auth priority:
  1. Gemini CLI (OAuth via Google account / Google One subscription) — free tier, no per-token cost
  2. REST API fallback (API key)  — used if CLI not installed or not authenticated

All callers use call() — auth selection is automatic and transparent.

Usage:
    from gemini_client import call

    text, in_tok, out_tok = call(
        model_key='reasoning',   # 'fast' | 'balanced' | 'reasoning'
        prompt='...',
    )
"""

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import requests
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass  # python-dotenv not installed; rely on shell environment

logger = logging.getLogger(__name__)

# ── API key (fallback only — loaded from .env, never hardcoded) ───────────────
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "")
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# ── Model tiers ────────────────────────────────────────────────────────────────
MODEL_CONFIG = {
    'fast': {
        'model_id':        'gemini-2.5-flash',
        'thinking_budget': 0,
        'max_output_tokens': 8192,
        'temperature':     0.4,
    },
    'balanced': {
        'model_id':        'gemini-2.5-flash',
        'thinking_budget': 8000,
        'max_output_tokens': 8192,
        'temperature':     1.0,
    },
    'reasoning': {
        'model_id':        'gemini-2.5-pro',   # stable (1000+ RPD) — was gemini-3.1-pro-preview (25-50 RPD)
        'thinking_budget': -1,
        'max_output_tokens': 16384,
        'temperature':     1.0,
    },
    # Legacy keys used by gemini_analyzer — map to the right tier
    'flash': {
        'model_id':        'gemini-2.5-flash',
        'thinking_budget': 0,
        'max_output_tokens': 8192,
        'temperature':     0.4,
    },
    'pro': {
        'model_id':        'gemini-2.5-pro',
        'thinking_budget': -1,
        'max_output_tokens': 16384,
        'temperature':     1.0,
    },
}

# ── Quota tracking ─────────────────────────────────────────────────────────────
_DB_PATH = Path(__file__).parent / 'trading_data' / 'trading_history.db'

# Rolling-24h soft limits (conservative vs. actual RPD — auto-downgrade kicks in before hard limit)
QUOTA_SOFT_LIMITS = {
    'gemini-2.5-pro':    800,   # stable: ~1000 RPD actual
    'gemini-2.5-flash':  700,   # stable: ~1000 RPD actual
}
QUOTA_WARN_PCT  = 0.75   # warn at 75% of soft limit
QUOTA_BLOCK_PCT = 0.95   # downgrade at 95% of soft limit

def _ensure_call_log(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gemini_call_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            model_id     TEXT NOT NULL,
            model_key    TEXT NOT NULL,
            caller       TEXT DEFAULT 'unknown',
            tokens_in    INTEGER DEFAULT 0,
            tokens_out   INTEGER DEFAULT 0,
            downgraded   INTEGER DEFAULT 0,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gcl_model_time ON gemini_call_log(model_id, created_at)")
    conn.commit()

def _log_call(model_id: str, model_key: str, caller: str,
              tokens_in: int, tokens_out: int, downgraded: bool = False):
    """Write one row to gemini_call_log. Silent on any error — never break a caller."""
    try:
        conn = sqlite3.connect(str(_DB_PATH))
        _ensure_call_log(conn)
        conn.execute(
            "INSERT INTO gemini_call_log (model_id, model_key, caller, tokens_in, tokens_out, downgraded) "
            "VALUES (?,?,?,?,?,?)",
            (model_id, model_key, caller, tokens_in, tokens_out, int(downgraded))
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # quota logging must never crash a caller

def get_rolling_usage(hours: int = 24) -> dict:
    """
    Return call counts and token totals per model_id for the last N hours.
    Example: {'gemini-2.5-pro': {'calls': 12, 'tokens_in': 84000, 'tokens_out': 9600},
              'gemini-2.5-flash': {'calls': 45, ...}}
    """
    try:
        conn = sqlite3.connect(str(_DB_PATH))
        conn.row_factory = sqlite3.Row
        _ensure_call_log(conn)
        rows = conn.execute("""
            SELECT model_id,
                   COUNT(*)        AS calls,
                   SUM(tokens_in)  AS tokens_in,
                   SUM(tokens_out) AS tokens_out
            FROM gemini_call_log
            WHERE created_at >= datetime('now', ?)
            GROUP BY model_id
        """, (f'-{hours} hours',)).fetchall()
        conn.close()
        return {r['model_id']: dict(r) for r in rows}
    except Exception:
        return {}

def check_quota(model_key: str) -> str:
    """
    Check rolling-24h usage against soft limits for the given model_key.
    Returns: 'ok' | 'warn' | 'block'
    'block' means the caller should downgrade to a cheaper tier.
    """
    model_id = MODEL_CONFIG.get(model_key, {}).get('model_id', '')
    limit = QUOTA_SOFT_LIMITS.get(model_id)
    if not limit:
        return 'ok'
    usage = get_rolling_usage(24)
    calls = usage.get(model_id, {}).get('calls', 0)
    ratio = calls / limit
    if ratio >= QUOTA_BLOCK_PCT:
        return 'block'
    if ratio >= QUOTA_WARN_PCT:
        return 'warn'
    return 'ok'


# ── Market session context block (injected into all AI prompts) ───────────────

try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    _ET_ZONE = _ZoneInfo('America/New_York')
except ImportError:
    _ET_ZONE = None   # Python < 3.9 fallback — context block will degrade gracefully

def market_context_block(vix: Optional[float] = None) -> str:
    """
    Return a formatted string block describing the current market session state.
    Inject this into every AI prompt so models know whether markets are open,
    what day it is, and whether price/VIX data is live or stale.

    Example output (weekend):
        ═══ MARKET SESSION CONTEXT ════════════════════════════════
          Date/Time (ET): Saturday 2026-03-01 01:15 AM ET
          Session status: ⛔ WEEKEND — US markets CLOSED
          Next open:      Monday 2026-03-03 09:30 AM ET
          Data freshness: VIX / prices are from Friday's close — NOT live
          ⚠️  This is an overnight/weekend monitoring cycle.
              Do NOT treat stale data as live market conditions.
              Do NOT simulate intraday trading or react to "rising VIX" as if
              markets are open. Focus on structural signals only.
        ════════════════════════════════════════════════════════════

    Example output (market open):
        ═══ MARKET SESSION CONTEXT ════════════════════════════════
          Date/Time (ET): Monday 2026-03-03 10:45 AM ET
          Session status: ✅ OPEN — regular session (09:30–16:00 ET)
          Data freshness: Live — prices and VIX are current
        ════════════════════════════════════════════════════════════
    """
    try:
        if _ET_ZONE:
            now = datetime.now(_ET_ZONE)
        else:
            now = datetime.utcnow()   # degraded — no timezone support

        day_name   = now.strftime('%A')       # 'Saturday', 'Monday', …
        date_str   = now.strftime('%Y-%m-%d')
        time_str   = now.strftime('%I:%M %p').lstrip('0')
        weekday    = now.weekday()            # 0=Mon … 4=Fri, 5=Sat, 6=Sun
        hour       = now.hour
        minute     = now.minute

        # Determine US regular session status
        _is_weekday = weekday < 5
        _after_open = (hour > 9) or (hour == 9 and minute >= 30)
        _before_close = hour < 16
        market_open = _is_weekday and _after_open and _before_close

        # Calculate next open
        if market_open:
            next_open_str = "currently open"
        elif _is_weekday and not _after_open:
            next_open_str = f"today ({day_name}) at 09:30 AM ET"
        elif _is_weekday and not _before_close:
            # After close on a weekday — next open is next weekday
            days_until = 3 if weekday == 4 else 1   # Friday→Monday, else +1
            from datetime import timedelta as _td
            next_day = now + _td(days=days_until)
            next_open_str = next_day.strftime('%A %Y-%m-%d') + ' at 09:30 AM ET'
        else:
            # Weekend
            days_until = (7 - weekday) % 7   # days until Monday (weekday=0)
            if days_until == 0:
                days_until = 7
            from datetime import timedelta as _td
            next_day = now + _td(days=days_until)
            next_open_str = next_day.strftime('%A %Y-%m-%d') + ' at 09:30 AM ET'

        vix_str = f"  VIX reference: {vix:.2f}\n" if vix is not None else ""

        if market_open:
            status_line  = '✅ OPEN — regular US session (NYSE/NASDAQ 09:30–16:00 ET)'
            freshness    = 'Live — prices and VIX are current'
            warning_text = ''
        else:
            if not _is_weekday:
                reason = 'WEEKEND'
            elif not _after_open:
                reason = 'PRE-MARKET'
            else:
                reason = 'AFTER HOURS'
            status_line  = f'⛔ {reason} — US markets CLOSED'
            freshness    = f'Stale — VIX/prices reflect the most recent session close, NOT live data'
            warning_text = (
                '  ⚠️  IMPORTANT: This is an outside-hours monitoring cycle.\n'
                '      Do NOT treat stale price or VIX readings as live market conditions.\n'
                '      Do NOT describe the market as actively trading, rising, or falling.\n'
                '      Do NOT suggest intraday actions. Focus on structural signals,\n'
                '      overnight news flow, and setup for the next open.\n'
            )

        block = (
            '═══ MARKET SESSION CONTEXT ════════════════════════════════\n'
            f'  Date/Time (ET): {day_name} {date_str} {time_str} ET\n'
            f'  Session status: {status_line}\n'
            + (f'  Next open:      {next_open_str}\n' if not market_open else '')
            + f'  Data freshness: {freshness}\n'
            + vix_str
            + warning_text
            + '════════════════════════════════════════════════════════════\n'
        )
        return block

    except Exception as _e:
        # Never crash a caller — return a minimal safe fallback
        return f'[market_context_block error: {_e}]\n'


# ── CLI availability check (cached after first call) ──────────────────────────
_cli_path: Optional[str] = None
_cli_authenticated: Optional[bool] = None
_last_cli_error: str = ''        # populated on CLI failure; inspected in call() for quota detection

def _get_cli_status() -> Tuple[bool, str]:
    """
    Returns (available, reason).
    CLI is usable if:
      - `gemini` binary is on PATH
      - ~/.gemini/oauth_creds.json exists with a refresh_token (survives restarts)
    """
    global _cli_path, _cli_authenticated

    if _cli_authenticated is not None:
        return _cli_authenticated, _cli_path or ''

    binary = shutil.which('gemini')
    if not binary:
        _cli_authenticated = False
        logger.debug("Gemini CLI not found on PATH — using REST API")
        return False, 'CLI not installed'

    creds_path = Path.home() / '.gemini' / 'oauth_creds.json'
    if not creds_path.exists():
        _cli_authenticated = False
        logger.debug("No OAuth creds found — using REST API")
        return False, 'Not authenticated'

    try:
        creds = json.loads(creds_path.read_text())
        if not creds.get('refresh_token'):
            _cli_authenticated = False
            return False, 'No refresh token'
    except Exception:
        _cli_authenticated = False
        return False, 'Creds unreadable'

    _cli_path = binary
    _cli_authenticated = True
    logger.debug(f"Gemini CLI authenticated at {binary}")
    logger.info(f"Using Gemini CLI binary: {binary}") # Elevated log level for visibility
    return True, binary


# ── Main call interface ────────────────────────────────────────────────────────

def call(
    prompt: str,
    model_key: str = 'fast',
    model_id: Optional[str] = None,   # override model_key if set
    temperature: Optional[float] = None,
    thinking_budget: Optional[int] = None,
    max_output_tokens: Optional[int] = None,
    caller: str = 'unknown',           # tag for quota tracking (e.g. 'analyst', 'broker_gate')
) -> Tuple[Optional[str], int, int]:
    """
    Call Gemini with automatic OAuth → API key fallback.
    Returns (text, input_tokens, output_tokens).
    text is None on failure.
    """
    cfg = dict(MODEL_CONFIG.get(model_key, MODEL_CONFIG['fast']))
    if model_id:
        cfg['model_id'] = model_id
    if temperature is not None:
        cfg['temperature'] = temperature
    if thinking_budget is not None:
        cfg['thinking_budget'] = thinking_budget
    if max_output_tokens is not None:
        cfg['max_output_tokens'] = max_output_tokens

    downgraded = False
    cli_ok, cli_info = _get_cli_status()

    if cli_ok:
        result = _call_via_cli(prompt, cfg)
        if result[0] is not None:
            _log_call(cfg['model_id'], model_key, caller, result[1], result[2])
            return result
        # Quota exhausted on Pro/Reasoning? Auto-downgrade to balanced rather than silently failing.
        # Matches both "TerminalQuotaError" (hard daily limit) and "No capacity available" (429 throttle).
        _pro_model = MODEL_CONFIG['reasoning']['model_id']
        _is_quota_err = ('TerminalQuotaError' in _last_cli_error
                         or 'exhausted your capacity' in _last_cli_error
                         or 'No capacity available' in _last_cli_error
                         or ('"code": 429' in _last_cli_error or "'code': 429" in _last_cli_error))
        if cfg['model_id'] == _pro_model and _is_quota_err:
            logger.warning("⚠️  Reasoning quota exhausted — auto-downgrading to balanced tier")
            cfg = dict(MODEL_CONFIG['balanced'])
            downgraded = True
            result = _call_via_cli(prompt, cfg)
            if result[0] is not None:
                _log_call(cfg['model_id'], model_key, caller, result[1], result[2], downgraded=True)
                return result
        # CLI call failed for another reason — fall through to REST API
        logger.warning("CLI call failed, falling back to REST API")

    result = _call_via_api(prompt, cfg)
    if result[0] is not None:
        _log_call(cfg['model_id'], model_key, caller, result[1], result[2], downgraded)
    return result


# ── CLI path ───────────────────────────────────────────────────────────────────

def _call_via_cli(prompt: str, cfg: dict) -> Tuple[Optional[str], int, int]:
    """Call via `gemini -p ... --output-format json`. OAuth is used automatically."""
    try:
        cmd = [
            _cli_path or 'gemini',
            '-p', prompt,
            '--model', cfg['model_id'],
            '--output-format', 'json',
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
            stdin=subprocess.DEVNULL, # prevent hanging on TTY requests
            env={**os.environ, 'GEMINI_API_KEY': ''},  # blank API key forces OAuth
        )

        if result.returncode != 0:
            global _last_cli_error
            err_msg = (result.stderr or '').strip()
            _last_cli_error = err_msg
            logger.warning(f"CLI exited {result.returncode} | Error: {err_msg[:200]}")
            return None, 0, 0

        data = json.loads(result.stdout)
        text = data.get('response', '').strip()

        # Extract token counts from stats
        stats    = data.get('stats', {})
        models   = stats.get('models', {})
        model_stats = models.get(cfg['model_id'], {})
        tok      = model_stats.get('tokens', {})
        in_tok   = tok.get('input', tok.get('prompt', 0))
        out_tok  = tok.get('candidates', 0)

        if not text:
            logger.warning("CLI returned empty response")
            return None, 0, 0

        logger.debug(f"CLI ✅ {cfg['model_id']} | in={in_tok} out={out_tok}")
        return text, in_tok, out_tok

    except subprocess.TimeoutExpired:
        logger.error("CLI call timed out after 180s")
        return None, 0, 0
    except json.JSONDecodeError as e:
        logger.error(f"CLI JSON parse error: {e}")
        return None, 0, 0
    except Exception as e:
        logger.error(f"CLI call error: {e}")
        return None, 0, 0


# ── REST API path ──────────────────────────────────────────────────────────────

def _call_via_api(prompt: str, cfg: dict) -> Tuple[Optional[str], int, int]:
    """Call via REST API with API key. Supports thinkingConfig.
    Only used as fallback if CLI is unavailable. Primary auth is OAuth via Gemini CLI."""
    if not GEMINI_API_KEY:
        logger.debug("REST API skipped — no GEMINI_API_KEY set (OAuth-only mode)")
        return None, 0, 0
    model_id = cfg['model_id']
    url = f"{GEMINI_API_BASE}/{model_id}:generateContent?key={GEMINI_API_KEY}"

    gen_config: dict = {
        'temperature':     cfg['temperature'],
        'maxOutputTokens': cfg['max_output_tokens'],
    }
    if cfg.get('thinking_budget', 0) != 0:
        gen_config['thinkingConfig'] = {'thinkingBudget': cfg['thinking_budget']}

    payload = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': gen_config,
    }

    try:
        resp = requests.post(url, json=payload, timeout=180)
        resp.raise_for_status()
        data = resp.json()

        cand   = data.get('candidates', [{}])[0]
        parts  = cand.get('content', {}).get('parts', [])
        # Filter out internal thought parts
        output = [p for p in parts if 'text' in p and not p.get('thought', False)]
        text   = ''.join(p['text'] for p in output).strip()

        usage  = data.get('usageMetadata', {})
        in_tok  = usage.get('promptTokenCount', 0)
        out_tok = usage.get('candidatesTokenCount', 0)
        tht_tok = usage.get('thoughtsTokenCount', 0)

        if not text:
            logger.warning(f"API returned empty output | finish={cand.get('finishReason')} | thought={tht_tok}tok")
            return None, in_tok, out_tok

        logger.debug(f"API ✅ {model_id} | in={in_tok} thought={tht_tok} out={out_tok}")
        return text, in_tok, out_tok

    except Exception as e:
        logger.error(f"REST API call failed ({model_id}): {e}")
        return None, 0, 0


# ── Convenience: reset cached CLI status (useful in tests) ────────────────────

def reset_cli_cache():
    global _cli_path, _cli_authenticated
    _cli_path = None
    _cli_authenticated = None
