#!/usr/bin/env python3
"""
grok_client.py — Refactored xAI Grok interface.
Supports deep reasoning for second opinions and real-time X.com analysis.
"""

import os
import json
import logging
import requests
from pathlib import Path
from typing import Optional, Tuple, Dict, Any
import time
import random

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

logger = logging.getLogger(__name__)

# Shared requests Session to avoid socket leaks from ad-hoc requests
_SESSION = requests.Session()

# Which system is running this client (mirrors gemini_client.py convention)
SYSTEM_NAME: str = os.environ.get("HIGHTRADE_SYSTEM", "hightrade")

# Cross-process rate limiter — prevents API burst when both trading systems run
try:
    from ai_choreographer import AIChoreographer as _Choreographer
    _CHOREOGRAPHER_OK = True
except ImportError:
    _CHOREOGRAPHER_OK = False
    logger.warning("[grok_client] ai_choreographer not found — no Grok rate limiting active")

class GrokClient:
    """Unified client for xAI Grok API."""
    
    def __init__(self):
        self.api_key = os.environ.get("XAI_API_KEY", "")
        self.base_url = "https://api.x.ai/v1"
        self.default_model = "grok-4-1-fast-reasoning"  # Preferred 4.1 reasoning model for analysis

    def _post_json_with_backoff(self, url: str, json_payload: dict, timeout: int = 180, max_retries: int = 5):
        """POST helper that reuses the module Session, closes responses, and backs off on 429s.
        Fails immediately on 400/401/403 (auth/key errors) — no point retrying those.
        Uses exponential backoff with jitter: 1s, 2s, 4s, 8s, 16s + random 0-2s jitter.
        """
        backoff = 1.0
        for attempt in range(1, max_retries + 1):
            resp = None
            try:
                resp = _SESSION.post(url, headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}, json=json_payload, timeout=timeout)
                if resp.status_code == 429:
                    # Rate limited — close socket before sleeping to prevent FD leak
                    jitter = random.uniform(0.0, 2.0)
                    sleep_for = backoff + jitter
                    logger.warning(f"Grok 429 on attempt {attempt}/{max_retries}. Backing off {sleep_for:.1f}s")
                    try:
                        resp.close()
                    except Exception:
                        pass
                    resp = None
                    time.sleep(sleep_for)
                    backoff *= 2
                    continue
                if resp.status_code in (400, 401, 403):
                    # Auth/key error — retrying won't help, bail immediately
                    logger.error(f"Grok API Error: {resp.status_code} - {resp.text}")
                    try:
                        resp.close()
                    except Exception:
                        pass
                    return None
                return resp
            except Exception:
                # Ensure any partially opened connection is closed
                if resp is not None:
                    try:
                        resp.close()
                    except Exception:
                        pass
                raise
        # Final attempt
        return resp

    def call(self, prompt: str, system_prompt: Optional[str] = None, 
             model_id: Optional[str] = None, temperature: float = 0.4) -> Tuple[Optional[str], int, int]:
        """Generic chat completion call."""
        if not self.api_key:
            logger.warning("Grok API skipped — no XAI_API_KEY set")
            return None, 0, 0

        model = model_id or self.default_model
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        # Cross-process RPM pacing (Grok had no rate limiting before)
        if _CHOREOGRAPHER_OK:
            _Choreographer.pace_and_record(model, SYSTEM_NAME)

        try:
            resp = self._post_json_with_backoff(
                f"{self.base_url}/chat/completions",
                {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "stream": False
                },
                timeout=180
            )

            if resp is None:
                logger.error("Grok API Error: no response (backoff/exhausted)")
                return None, 0, 0

            try:
                if resp.status_code != 200:
                    logger.error(f"Grok API Error: {resp.status_code} - {resp.text}")
                    return None, 0, 0

                data = resp.json()
                text = data.get('choices', [{}])[0].get('message', {}).get('content', '').strip()
                usage = data.get('usage', {})
                in_tok = usage.get('prompt_tokens', 0)
                out_tok = usage.get('completion_tokens', 0)

                logger.debug(f"Grok ✅ {model} | in={in_tok} out={out_tok}")
                return text, in_tok, out_tok
            finally:
                try:
                    resp.close()
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Grok call failed: {e}")
            return None, 0, 0

    def call_with_search(self, prompt: str, system_prompt: Optional[str] = None,
                         model_id: Optional[str] = None, temperature: float = 0.4,
                         use_web_search: bool = True, use_x_search: bool = True) -> Tuple[Optional[str], int, int]:
        """Call Grok via the Responses API with web_search and/or x_search tools.

        The Responses API at /v1/responses supports server-side search tools,
        unlike the chat completions endpoint. Returns (text, in_tok, out_tok).
        """
        if not self.api_key:
            logger.warning("Grok API skipped — no XAI_API_KEY set")
            return None, 0, 0

        model = model_id or "grok-4-1-fast-non-reasoning"

        tools = []
        if use_web_search:
            tools.append({"type": "web_search"})
        if use_x_search:
            tools.append({"type": "x_search"})

        input_messages = []
        if system_prompt:
            input_messages.append({"role": "system", "content": system_prompt})
        input_messages.append({"role": "user", "content": prompt})

        if _CHOREOGRAPHER_OK:
            _Choreographer.pace_and_record(model, SYSTEM_NAME)

        try:
            resp = self._post_json_with_backoff(
                f"{self.base_url}/responses",
                {
                    "model": model,
                    "input": input_messages,
                    "tools": tools,
                    "temperature": temperature,
                },
                timeout=120
            )

            if resp is None:
                logger.error("Grok Responses API Error: no response (backoff/exhausted)")
                return None, 0, 0

            try:
                if resp.status_code != 200:
                    logger.error(f"Grok Responses API Error: {resp.status_code} - {resp.text[:300]}")
                    logger.info("  Falling back to chat completions (no search)...")
                    return self.call(prompt, system_prompt=system_prompt, temperature=temperature)

                data = resp.json()

                # Responses API returns an 'output' array — extract text from message items
                text_parts = []
                for item in data.get('output', []):
                    if item.get('type') == 'message':
                        for content in item.get('content', []):
                            if content.get('type') == 'output_text':
                                text_parts.append(content.get('text', ''))

                text = '\n'.join(text_parts).strip()
                usage = data.get('usage', {})
                in_tok = usage.get('input_tokens', 0)
                out_tok = usage.get('output_tokens', 0)

                logger.debug(f"Grok Responses ✅ {model} | in={in_tok} out={out_tok}")
                return text, in_tok, out_tok
            finally:
                try:
                    resp.close()
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Grok Responses API call failed: {e}")
            logger.info("  Falling back to chat completions...")
            return self.call(prompt, system_prompt=system_prompt, temperature=temperature)

    def second_opinion(self, payload: Dict[str, Any], focus: str = "current positions/watchlist") -> Optional[Dict]:
        """Deep reasoning second opinion with X-powered critique."""
        system_prompt = """
        You are Grok as independent second-opinion analyst for the HighTrade system.
        Lead model is Gemini 3.1 Pro.
        - Be truth-seeking: flag blind spots, over-optimism, missed signals.
        - Leverage real-time X data for sentiment, flow, whispers.
        - Output strict JSON only.
        """
        
        prompt = f"STATE SNAPSHOT:\n{json.dumps(payload, indent=2)}\n\nFocus: {focus}\n\nProvide a second opinion in JSON format with keys: critique (str), x_signals (list of dicts), gaps_recommendations (list of str), action_suggestion (hold|buy|sell|monitor|add_to_watch), and confidence (float 0-1)."
        
        text, in_tok, out_tok = self.call(prompt, system_prompt=system_prompt, temperature=0.4)
        if not text:
            return None

        # Clean JSON markdown if present
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        try:
            result = json.loads(text)
            result['input_tokens'] = in_tok
            result['output_tokens'] = out_tok
            return result
        except json.JSONDecodeError:
            logger.error(f"Failed to parse Grok second opinion JSON: {text[:200]}")
            return None

# Backward compatibility for existing functional calls
_instance = None
def call(*args, **kwargs):
    global _instance
    if _instance is None:
        _instance = GrokClient()
    return _instance.call(*args, **kwargs)

if __name__ == "__main__":
    client = GrokClient()
    print(f"Testing Grok Client with {client.default_model}...")
    res = client.second_opinion({"market_regime": "bullish", "defcon": 5}, focus="SPY")
    print(json.dumps(res, indent=2) if res else "Failed")
