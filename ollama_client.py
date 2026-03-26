#!/usr/bin/env python3
"""
ollama_client.py — Local Ollama LLM interface

NOTE: This module is NOT used by highTRADE and must NOT be imported by any
trading component (orchestrator, analyst, broker, etc.).  It is kept here
for reference / future cross-project use and is the intended client for the
OpenClaw project.  Do not wire it into any highTRADE pipeline.

Architecture:
  ┌────────────────────────────────────────────────────────────────────┐
  │ 1. CONFIG        Models, base URL, timeouts                       │
  │ 2. Tool parsing  Handles both tool_calls field AND JSON-in-content │
  │ 3. OllamaClient  Core chat/tool-call interface                    │
  │ 4. SubAgent      Named sub-agent registry + dispatch              │
  │ 5. call()        Drop-in compatible with gemini_client.call()     │
  └────────────────────────────────────────────────────────────────────┘

Tool-call parsing strategy (handles model quirks):
  Priority 1 → message.tool_calls[]              (llama3.1, mistral, etc.)
  Priority 2 → message.content as raw JSON        (qwen2.5-coder fallback)
  Priority 3 → text response (no tool use needed)

Sub-agent types:
  analyst     → acquisition_analyst.run_analyst_cycle
  researcher  → acquisition_researcher.run_research_cycle
  hound       → acquisition_hound (Grok alpha scan)
  verifier    → acquisition_verifier
  exit        → exit_analyst
  broker      → broker_agent conditionals check
  briefing    → daily_briefing / flash briefing
  news        → news_signals / news_aggregator

Usage:
    from ollama_client import OllamaClient, call, dispatch_tool

    # Simple text generation
    text = call("What is the current market regime?")

    # With tools (sub-agent dispatch)
    result = dispatch_tool("run analyst on AAPL", tools=SUB_AGENT_TOOLS)

    # Direct client
    client = OllamaClient()
    response = client.chat("Analyze NVDA", tools=[...])
"""

import json
import logging
import os
import re
import requests
import time
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# 1. CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

OLLAMA_BASE_URL  = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_TIMEOUT   = int(os.environ.get("OLLAMA_TIMEOUT", "180"))

# Model aliases — override via env or pass model= to OllamaClient()
# llama3.2:3b: 2GB, proper tool_calls, ~8 tok/s on Intel CPU — best for this hardware
# llama3.1:8b: 5GB, also has tool_calls but too slow on Intel i3 (no Metal/GPU)
# qwen2.5-coder: coding fallback only (embeds tool JSON in content field)
_PREFERRED_ORCHESTRATOR = "llama3.2:3b"
_FALLBACK_MODEL         = "qwen2.5-coder:7b-instruct-q8_0"

OLLAMA_ORCHESTRATOR_MODEL = os.environ.get(
    "OLLAMA_ORCHESTRATOR_MODEL", _PREFERRED_ORCHESTRATOR
)

# Per-thread session to avoid socket leaks
_session_local = threading.local()

def _get_session() -> requests.Session:
    if not hasattr(_session_local, "session") or _session_local.session is None:
        _session_local.session = requests.Session()
    return _session_local.session


# ═══════════════════════════════════════════════════════════════════════════════
# 2. TOOL-CALL PARSING  (handles both proper tool_calls AND JSON-in-content)
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_tool_calls(message: Dict) -> List[Dict]:
    """
    Normalize tool call output from any Ollama model into a consistent list:
        [{"name": "tool_name", "arguments": {...}}, ...]

    Handles three output styles:
      A) message.tool_calls = [{"function": {"name": ..., "arguments": ...}}]
         → llama3.1, mistral, phi4, etc. (proper Ollama tool_calls format)

      B) message.content = '{"name": "...", "arguments": {...}}'
         → qwen2.5-coder and other models that embed tool JSON in content

      C) message.content = 'Some text without any tool call'
         → plain text response, returns []
    """
    # Style A: proper tool_calls field
    raw_tc = message.get("tool_calls")
    if raw_tc:
        normalized = []
        for tc in raw_tc:
            fn = tc.get("function", tc)  # some models nest under "function"
            name = fn.get("name", "")
            args = fn.get("arguments", fn.get("parameters", {}))

            # Unwrap llama3.2 quirk: args arrive as {"object": "{...json...}"}
            # or {"Return": "{}"} instead of the actual argument dict
            if isinstance(args, dict):
                if "object" in args and isinstance(args["object"], str):
                    try:
                        args = json.loads(args["object"])
                    except json.JSONDecodeError:
                        args = {}
                elif list(args.keys()) == ["Return"]:
                    args = {}  # no-argument function
            elif isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"raw": args}

            if name:
                normalized.append({"name": name, "arguments": args})
        if normalized:
            return normalized

    # Style B: JSON-in-content fallback (qwen2.5-coder etc.)
    content = message.get("content", "").strip()
    if content:
        # Try direct JSON parse
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict) and "name" in parsed:
                args = parsed.get("arguments", parsed.get("parameters", {}))
                return [{"name": parsed["name"], "arguments": args}]
        except json.JSONDecodeError:
            pass

        # Try extracting JSON from a markdown code block
        code_block = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
        if code_block:
            try:
                parsed = json.loads(code_block.group(1))
                if isinstance(parsed, dict) and "name" in parsed:
                    args = parsed.get("arguments", {})
                    return [{"name": parsed["name"], "arguments": args}]
            except json.JSONDecodeError:
                pass

    # Style C: plain text, no tool call
    return []


# ═══════════════════════════════════════════════════════════════════════════════
# 3. OLLAMA CLIENT
# ═══════════════════════════════════════════════════════════════════════════════

class OllamaClient:
    """
    Thin wrapper around Ollama's /api/chat endpoint.

    Automatically:
      - Falls back to qwen2.5-coder if preferred model isn't loaded
      - Parses tool calls from both tool_calls field and JSON-in-content
      - Retries on transient errors with exponential backoff
    """

    def __init__(
        self,
        model: Optional[str] = None,
        base_url: str = OLLAMA_BASE_URL,
        timeout: int = OLLAMA_TIMEOUT,
        system_prompt: Optional[str] = None,
    ):
        self.base_url     = base_url.rstrip("/")
        self.timeout      = timeout
        self.system_prompt = system_prompt
        self._model       = model  # None = auto-select at first call
        self._available   = None  # cached list of available model names

    # ── Model selection ───────────────────────────────────────────────────────

    def _get_available_models(self) -> List[str]:
        if self._available is not None:
            return self._available
        try:
            resp = _get_session().get(f"{self.base_url}/api/tags", timeout=5)
            resp.raise_for_status()
            self._available = [m["name"] for m in resp.json().get("models", [])]
            logger.debug(f"[OllamaClient] Available models: {self._available}")
        except Exception as e:
            logger.warning(f"[OllamaClient] Could not list models: {e}")
            self._available = []
        return self._available

    def _resolve_model(self) -> str:
        """Return preferred model if loaded, else fall back."""
        if self._model:
            return self._model
        available = self._get_available_models()
        if not available:
            return OLLAMA_ORCHESTRATOR_MODEL  # best-effort
        # Exact match
        if OLLAMA_ORCHESTRATOR_MODEL in available:
            return OLLAMA_ORCHESTRATOR_MODEL
        # Prefix match (e.g. "llama3.1:8b" in "llama3.1:8b-instruct-q4_0")
        for m in available:
            if m.startswith(OLLAMA_ORCHESTRATOR_MODEL.split(":")[0]):
                logger.info(f"[OllamaClient] Using {m} (preferred={OLLAMA_ORCHESTRATOR_MODEL})")
                return m
        # Fall back to first available
        logger.warning(
            f"[OllamaClient] {OLLAMA_ORCHESTRATOR_MODEL} not found. "
            f"Using fallback: {available[0]}"
        )
        return available[0]

    # ── Core chat ─────────────────────────────────────────────────────────────

    def chat(
        self,
        prompt: str,
        tools: Optional[List[Dict]] = None,
        history: Optional[List[Dict]] = None,
        temperature: float = 0.1,
        max_retries: int = 2,
    ) -> Dict:
        """
        Send a chat message and return a normalized response dict:
        {
            "content":    str,           # text content (may be empty if tool called)
            "tool_calls": [...],         # normalized tool calls (may be empty)
            "model":      str,
            "done":       bool,
            "raw":        dict,          # full raw Ollama response
        }
        """
        model = self._resolve_model()
        messages = []

        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": prompt})

        payload: Dict[str, Any] = {
            "model":   model,
            "messages": messages,
            "stream":  False,
            "options": {"temperature": temperature},
        }
        if tools:
            payload["tools"] = tools

        last_err = None
        for attempt in range(1, max_retries + 2):
            try:
                resp = _get_session().post(
                    f"{self.base_url}/api/chat",
                    json=payload,
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                raw = resp.json()

                message = raw.get("message", {})
                tool_calls = _extract_tool_calls(message)

                # If tool call was embedded in content, clear content so callers
                # don't double-process
                content = message.get("content", "")
                if tool_calls and content:
                    # Only clear content if it looks like it IS the tool call JSON
                    try:
                        parsed = json.loads(content.strip())
                        if isinstance(parsed, dict) and "name" in parsed:
                            content = ""
                    except (json.JSONDecodeError, ValueError):
                        pass

                return {
                    "content":    content,
                    "tool_calls": tool_calls,
                    "model":      raw.get("model", model),
                    "done":       raw.get("done", True),
                    "raw":        raw,
                }

            except requests.exceptions.Timeout:
                last_err = f"timeout after {self.timeout}s"
            except requests.exceptions.ConnectionError:
                last_err = f"connection refused — is Ollama running at {self.base_url}?"
                break  # no point retrying if Ollama is down
            except Exception as e:
                last_err = str(e)

            if attempt <= max_retries:
                backoff = 2 ** (attempt - 1)
                logger.warning(
                    f"[OllamaClient] Attempt {attempt} failed ({last_err}), "
                    f"retrying in {backoff}s..."
                )
                time.sleep(backoff)

        raise RuntimeError(f"[OllamaClient] All attempts failed: {last_err}")

    # ── Convenience: plain text ───────────────────────────────────────────────

    def generate(self, prompt: str, **kwargs) -> str:
        """Simple text generation — returns content string only."""
        return self.chat(prompt, **kwargs)["content"]


# ═══════════════════════════════════════════════════════════════════════════════
# 4. SUB-AGENT REGISTRY + DISPATCH
# ═══════════════════════════════════════════════════════════════════════════════

# Tool definitions for the orchestrator — each tool maps to a HighTrade sub-agent
SUB_AGENT_TOOLS: List[Dict] = [
    {
        "type": "function",
        "function": {
            "name": "run_analyst",
            "description": (
                "Run the AI acquisition analyst on all library_ready tickers. "
                "Creates conditional_tracking entries for tickers above the confidence threshold."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "defcon_level": {
                        "type": "integer",
                        "description": "Current DEFCON level 1-5 (1=crisis, 5=calm)",
                    },
                    "news_score": {
                        "type": "number",
                        "description": "Current news signal score 0-100",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_researcher",
            "description": (
                "Run the acquisition researcher on all pending watchlist tickers. "
                "Gathers market data, financials, news, and prepares research for the analyst."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_hound",
            "description": (
                "Run the Grok Hound alpha scanner to find new trade candidates "
                "not already on the watchlist."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "defcon_level": {"type": "integer"},
                    "news_score":   {"type": "number"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_conditionals",
            "description": (
                "Check all active conditionals against live prices. "
                "Executes or notifies on any triggered entries."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "defcon_level": {"type": "integer"},
                    "news_score":   {"type": "number"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_exit_analyst",
            "description": (
                "Run the exit analyst on all open positions missing stop-loss "
                "or take-profit levels."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "defcon_level": {"type": "integer"},
                    "macro_score":  {"type": "number"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_verifier",
            "description": (
                "Run the acquisition verifier to confirm or invalidate active "
                "conditional entries based on fresh data."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_portfolio_status",
            "description": (
                "Return a summary of open positions, active conditionals, "
                "available cash, and current DEFCON level."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]

# ── Sub-agent executor functions ──────────────────────────────────────────────

def _exec_run_analyst(args: Dict) -> str:
    try:
        from acquisition_analyst import run_analyst_cycle
        context = {
            "defcon_level": args.get("defcon_level", 3),
            "news_score":   args.get("news_score", 50),
        }
        results = run_analyst_cycle(extra_context=context)
        entered = [r.get("_ticker", "?") for r in results if r and r.get("should_enter")]
        return f"Analyst cycle complete: {len(results)} analyzed, {len(entered)} conditional(s) set → {entered}"
    except Exception as e:
        return f"Analyst failed: {e}"


def _exec_run_researcher(args: Dict) -> str:
    try:
        from acquisition_researcher import run_research_cycle
        tickers = run_research_cycle()
        return f"Researcher cycle complete: {len(tickers)} tickers researched → {tickers}"
    except Exception as e:
        return f"Researcher failed: {e}"


def _exec_run_hound(args: Dict) -> str:
    try:
        from acquisition_hound import run_hound_cycle
        ctx = {
            "defcon_level": args.get("defcon_level", 3),
            "news_score":   args.get("news_score", 50),
        }
        result = run_hound_cycle(extra_context=ctx)
        candidates = result.get("candidates", [])
        return f"Hound scan complete: {len(candidates)} candidate(s) found → {[c.get('ticker') for c in candidates]}"
    except Exception as e:
        return f"Hound failed: {e}"


def _exec_check_conditionals(args: Dict) -> str:
    try:
        import sqlite3 as _sq
        from pathlib import Path as _P
        db = _P(__file__).parent / "trading_data" / "trading_history.db"
        conn = _sq.connect(str(db))
        rows = conn.execute(
            "SELECT ticker, entry_price_target, research_confidence, watch_tag "
            "FROM conditional_tracking WHERE status='active' ORDER BY research_confidence DESC"
        ).fetchall()
        conn.close()
        if not rows:
            return "No active conditionals to check."
        summary = [f"{r[0]} entry=${r[1]} conf={r[2]:.0%} [{r[3]}]" for r in rows]
        return f"{len(rows)} active conditional(s): " + " | ".join(summary)
    except Exception as e:
        return f"Conditionals check failed: {e}"


def _exec_run_exit_analyst(args: Dict) -> str:
    try:
        import exit_analyst
        processed = exit_analyst.run_exit_analysis(
            defcon=args.get("defcon_level", 3),
            macro_score=args.get("macro_score", 50),
        )
        return f"Exit analyst complete: {processed} position(s) analyzed"
    except Exception as e:
        return f"Exit analyst failed: {e}"


def _exec_run_verifier(args: Dict) -> str:
    try:
        from acquisition_verifier import run_verifier_cycle
        result = run_verifier_cycle()
        return f"Verifier complete: {result}"
    except Exception as e:
        return f"Verifier failed: {e}"


def _exec_get_portfolio_status(args: Dict) -> str:
    try:
        import sqlite3 as _sq
        from pathlib import Path as _P
        db = _P(__file__).parent / "trading_data" / "trading_history.db"
        conn = _sq.connect(str(db))
        positions = conn.execute(
            "SELECT asset_symbol, side, quantity, entry_price, stop_loss, take_profit_1 "
            "FROM trade_records WHERE status='open'"
        ).fetchall()
        conditionals = conn.execute(
            "SELECT ticker, entry_price_target, research_confidence "
            "FROM conditional_tracking WHERE status='active'"
        ).fetchall()
        defcon_row = conn.execute(
            "SELECT defcon_level FROM monitoring_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        defcon = defcon_row[0] if defcon_row else "?"
        pos_str  = ", ".join(f"{p[0]} x{p[2]}" for p in positions) or "none"
        cond_str = ", ".join(f"{c[0]}@${c[1]}" for c in conditionals) or "none"
        return (
            f"DEFCON {defcon} | "
            f"Open positions ({len(positions)}): {pos_str} | "
            f"Active conditionals ({len(conditionals)}): {cond_str}"
        )
    except Exception as e:
        return f"Portfolio status failed: {e}"


# Registry mapping tool name → executor function
_SUB_AGENT_REGISTRY: Dict[str, Any] = {
    "run_analyst":          _exec_run_analyst,
    "run_researcher":       _exec_run_researcher,
    "run_hound":            _exec_run_hound,
    "check_conditionals":   _exec_check_conditionals,
    "run_exit_analyst":     _exec_run_exit_analyst,
    "run_verifier":         _exec_run_verifier,
    "get_portfolio_status": _exec_get_portfolio_status,
}


def dispatch_tool(tool_name: str, arguments: Dict) -> str:
    """Execute a named sub-agent tool and return its result string."""
    fn = _SUB_AGENT_REGISTRY.get(tool_name)
    if not fn:
        available = list(_SUB_AGENT_REGISTRY.keys())
        return f"Unknown tool '{tool_name}'. Available: {available}"
    logger.info(f"[OllamaClient] Dispatching sub-agent: {tool_name}({arguments})")
    return fn(arguments)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. TOP-LEVEL ORCHESTRATION LOOP
# ═══════════════════════════════════════════════════════════════════════════════

_ORCHESTRATOR_SYSTEM = """\
You are HighTrade's local AI orchestrator. Your job is to decide which sub-agents
to run based on the current market situation and delegate to them using tools.

Guidelines:
- Always check portfolio status first if context is unclear.
- Run the analyst whenever there are library_ready research items.
- Run the researcher when the watchlist has pending tickers.
- Run the hound for new alpha when DEFCON is 3-5 (stable/recovering market).
- Check conditionals before making any new entry decisions.
- Run exit analyst when open positions lack stop/TP levels.
- Be decisive — pick one action per turn, execute it, observe the result.
- Keep responses concise: one sentence of reasoning + tool call.
"""


def orchestrate(
    objective: str,
    max_turns: int = 6,
    live_context: Optional[Dict] = None,
    client: Optional[OllamaClient] = None,
) -> List[Dict]:
    """
    Run a multi-turn agentic loop: Ollama decides which sub-agent to call,
    executes it, observes the result, and continues until the objective is met
    or max_turns is reached.

    Returns the conversation history.
    """
    if client is None:
        client = OllamaClient(system_prompt=_ORCHESTRATOR_SYSTEM)

    # Build context block for the initial prompt
    ctx_lines = []
    if live_context:
        for k, v in live_context.items():
            ctx_lines.append(f"  {k}: {v}")
    ctx_block = "\nCurrent context:\n" + "\n".join(ctx_lines) if ctx_lines else ""

    history: List[Dict] = []
    prompt = f"{objective}{ctx_block}"

    for turn in range(1, max_turns + 1):
        logger.info(f"[OllamaClient] Orchestration turn {turn}/{max_turns}")

        response = client.chat(prompt, tools=SUB_AGENT_TOOLS, history=history)
        content    = response["content"]
        tool_calls = response["tool_calls"]

        # Record assistant message in history
        history.append({"role": "assistant", "content": content or str(tool_calls)})

        if not tool_calls:
            # No tool call → treat as final answer
            logger.info(f"[OllamaClient] Orchestrator finished: {content[:120]}")
            break

        # Execute each tool call and collect results
        tool_results = []
        for tc in tool_calls:
            tool_name = tc.get("name", "")
            arguments = tc.get("arguments", {})
            logger.info(f"[OllamaClient] → calling {tool_name}({arguments})")

            result = dispatch_tool(tool_name, arguments)
            logger.info(f"[OllamaClient] ← {tool_name} result: {result[:120]}")
            tool_results.append(f"[{tool_name}] {result}")

            # Record tool result in history
            history.append({
                "role": "tool",
                "content": result,
                "name": tool_name,
            })

        # Feed results back as next prompt
        prompt = "Tool results:\n" + "\n".join(tool_results) + "\n\nContinue or report done."

    return history


# ═══════════════════════════════════════════════════════════════════════════════
# 6. PUBLIC HELPERS  (gemini_client-compatible surface)
# ═══════════════════════════════════════════════════════════════════════════════

_default_client: Optional[OllamaClient] = None
_client_lock = threading.Lock()


def _get_default_client() -> OllamaClient:
    global _default_client
    with _client_lock:
        if _default_client is None:
            _default_client = OllamaClient()
    return _default_client


def call(
    prompt: str,
    model: Optional[str] = None,
    tools: Optional[List[Dict]] = None,
    system_prompt: Optional[str] = None,
    temperature: float = 0.1,
) -> Tuple[str, int, int]:
    """
    Drop-in compatible with gemini_client.call() signature.
    Returns (text, prompt_tokens, completion_tokens).
    """
    client = OllamaClient(model=model, system_prompt=system_prompt)
    response = client.chat(prompt, tools=tools, temperature=temperature)
    raw = response.get("raw", {})
    in_tok  = raw.get("prompt_eval_count", 0)
    out_tok = raw.get("eval_count", 0)
    return response["content"], in_tok, out_tok


# ═══════════════════════════════════════════════════════════════════════════════
# CLI  — smoke test + interactive orchestration
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s:%(name)s: %(message)s",
    )

    client = OllamaClient()
    model  = client._resolve_model()
    print(f"\nOllamaClient smoke-test  (model={model})")
    print("=" * 60)

    # 1. Plain text
    print("\n[1] Plain text generation:")
    text = client.generate("In one sentence, what is a covered call option?")
    print(f"    {text}")

    # 2. Tool call parsing
    print("\n[2] Tool call dispatch (portfolio status):")
    resp = client.chat(
        "Check the portfolio status and tell me what conditionals are active.",
        tools=SUB_AGENT_TOOLS,
    )
    if resp["tool_calls"]:
        for tc in resp["tool_calls"]:
            result = dispatch_tool(tc["name"], tc["arguments"])
            print(f"    Tool: {tc['name']} → {result}")
    else:
        print(f"    (no tool call) Response: {resp['content'][:200]}")

    # 3. Full orchestration loop (if --orchestrate flag given)
    if "--orchestrate" in sys.argv:
        print("\n[3] Full orchestration loop:")
        history = orchestrate(
            objective="Check what analysis work is pending and run the most important sub-agent.",
            live_context={"defcon": 2, "news_score": 47},
            max_turns=4,
        )
        print("\n--- Conversation ---")
        for msg in history:
            role = msg.get("role", "?")
            content = msg.get("content", "")[:200]
            print(f"  [{role}] {content}")
