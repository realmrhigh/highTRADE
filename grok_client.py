#!/usr/bin/env python3
"""
grok_client.py — Unified Grok (xAI) call interface for HighTrade
Supports X.com analysis and second opinions next to Gemini.
"""

import json
import logging
import os
import requests
from pathlib import Path
from typing import Optional, Tuple

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

logger = logging.getLogger(__name__)

# ── API configuration ──────────────────────────────────────────────────────────
XAI_API_KEY = os.environ.get("XAI_API_KEY", "")
XAI_API_BASE = "https://api.x.ai/v1"

# ── Model configuration ────────────────────────────────────────────────────────
MODEL_CONFIG = {
    'grok-3': {
        'model_id': 'grok-3',
        'max_tokens': 4096,
        'temperature': 0.7,
    },
    'grok-2-vision': {
        'model_id': 'grok-2-vision-1212',
        'max_tokens': 4096,
        'temperature': 0.7,
    }
}

DEFAULT_MODEL = 'grok-3'

def call(
    prompt: str,
    system_prompt: Optional[str] = None,
    model_id: str = DEFAULT_MODEL,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> Tuple[Optional[str], int, int]:
    """
    Call Grok (xAI) API.
    Returns (text, input_tokens, output_tokens).
    text is None on failure.
    """
    if not XAI_API_KEY:
        logger.warning("Grok API skipped — no XAI_API_KEY set in .env")
        return None, 0, 0

    url = f"{XAI_API_BASE}/chat/completions"
    
    headers = {
        "Authorization": f"Bearer {XAI_API_KEY}",
        "Content-Type": "application/json"
    }

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model_id,
        "messages": messages,
        "temperature": temperature if temperature is not None else MODEL_CONFIG.get(model_id, {}).get('temperature', 0.7),
        "max_tokens": max_tokens if max_tokens is not None else MODEL_CONFIG.get(model_id, {}).get('max_tokens', 4096),
        "stream": False
    }

    try:
        logger.debug(f"Calling Grok ({model_id})...")
        response = requests.post(url, headers=headers, json=payload, timeout=180)
        
        if response.status_code != 200:
            logger.error(f"Grok API Error: {response.status_code} - {response.text}")
            return None, 0, 0
            
        data = response.json()

        text = data.get('choices', [{}])[0].get('message', {}).get('content', '').strip()
        usage = data.get('usage', {})
        in_tok = usage.get('prompt_tokens', 0)
        out_tok = usage.get('completion_tokens', 0)

        if not text:
            logger.warning(f"Grok returned empty output for model {model_id}")
            return None, in_tok, out_tok

        logger.debug(f"Grok ✅ {model_id} | in={in_tok} out={out_tok}")
        return text, in_tok, out_tok

    except Exception as e:
        logger.error(f"Grok API call failed ({model_id}): {e}")
        return None, 0, 0

if __name__ == "__main__":
    # Test call
    logging.basicConfig(level=logging.DEBUG)
    print("Testing Grok Client...")
    res, i, o = call("What's the current sentiment on X regarding NVDA earnings?")
    if res:
        print(f"Grok Response: {res[:200]}...")
        print(f"Tokens: in={i}, out={o}")
    else:
        print("Grok Call Failed (likely missing API key)")
