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
import subprocess
import requests
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
        'model_id':        'gemini-3-pro-preview',   # upgrade to gemini-3.1-pro-preview once CLI + API key support it
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
        'model_id':        'gemini-3-pro-preview',   # upgrade to gemini-3.1-pro-preview once CLI + API key support it
        'thinking_budget': -1,
        'max_output_tokens': 16384,
        'temperature':     1.0,
    },
}

# ── CLI availability check (cached after first call) ──────────────────────────
_cli_path: Optional[str] = None
_cli_authenticated: Optional[bool] = None

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
    return True, binary


# ── Main call interface ────────────────────────────────────────────────────────

def call(
    prompt: str,
    model_key: str = 'fast',
    model_id: Optional[str] = None,   # override model_key if set
    temperature: Optional[float] = None,
    thinking_budget: Optional[int] = None,
    max_output_tokens: Optional[int] = None,
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

    cli_ok, cli_info = _get_cli_status()

    if cli_ok:
        result = _call_via_cli(prompt, cfg)
        if result[0] is not None:
            return result
        # CLI call failed (expired token, rate limit, etc.) — fall through to API
        logger.warning("CLI call failed, falling back to REST API")

    return _call_via_api(prompt, cfg)


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
            env={**os.environ, 'GEMINI_API_KEY': ''},  # blank API key forces OAuth
        )

        if result.returncode != 0:
            logger.warning(f"CLI exited {result.returncode}: {result.stderr[:200]}")
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
