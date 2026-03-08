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

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

logger = logging.getLogger(__name__)

class GrokClient:
    """Unified client for xAI Grok API."""
    
    def __init__(self):
        self.api_key = os.environ.get("XAI_API_KEY", "")
        self.base_url = "https://api.x.ai/v1"
        self.default_model = "grok-4-1-fast-reasoning"  # As suggested by Grok

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

        try:
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "stream": False
                },
                timeout=180
            )
            
            if response.status_code != 200:
                logger.error(f"Grok API Error: {response.status_code} - {response.text}")
                return None, 0, 0

            data = response.json()
            text = data.get('choices', [{}])[0].get('message', {}).get('content', '').strip()
            usage = data.get('usage', {})
            in_tok = usage.get('prompt_tokens', 0)
            out_tok = usage.get('completion_tokens', 0)

            logger.debug(f"Grok ✅ {model} | in={in_tok} out={out_tok}")
            return text, in_tok, out_tok

        except Exception as e:
            logger.error(f"Grok call failed: {e}")
            return None, 0, 0

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
    text, in_tok, out_tok = client.call("Reply with a single JSON object: {\"status\": \"ok\"}")
    print(f"Response: {text} | tokens: {in_tok}→{out_tok}")
