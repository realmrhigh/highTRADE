#!/usr/bin/env python3
"""
test_fd_leaks.py — FD/socket leak regression tests for highTRADE AI components.

Tests verify:
  1. HTTP response objects are always closed (FD count returns to baseline).
  2. SQLite connections are always closed on exception paths.
  3. 429 responses trigger backoff and still close the socket.
  4. AlpacaBroker uses the module-level session (no per-call socket pool growth).

All tests use mock HTTP endpoints — no real network calls are made.

Run:
    python -m pytest test_fd_leaks.py -v
    # or
    python test_fd_leaks.py
"""

import json
import os
import resource
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock, patch, PropertyMock


# ─── helpers ──────────────────────────────────────────────────────────────────

def _open_fd_count() -> int:
    """Return the number of open file descriptors for this process (macOS/Linux)."""
    try:
        # macOS: use resource module soft limit as upper bound; count /dev/fd entries
        fd_dir = f"/proc/{os.getpid()}/fd"
        if os.path.isdir(fd_dir):
            return len(os.listdir(fd_dir))
        # macOS fallback: lsof
        import subprocess
        out = subprocess.check_output(
            ["lsof", "-p", str(os.getpid()), "-n", "-P"],
            stderr=subprocess.DEVNULL,
        )
        return len(out.splitlines()) - 1  # subtract header
    except Exception:
        return -1  # unavailable — tests that check FD count will skip


def _skip_if_fd_count_unavailable(test):
    """Decorator: skip test if FD counting is not supported on this platform."""
    def wrapper(self):
        if _open_fd_count() < 0:
            self.skipTest("FD counting not available on this platform")
        test(self)
    wrapper.__name__ = test.__name__
    return wrapper


# ─── mock HTTP server (local, no real network) ────────────────────────────────

class _MockHandler(BaseHTTPRequestHandler):
    """Tiny handler that serves pre-canned responses set on the class."""
    status_code  = 200
    body         = b'{"ok": true}'
    content_type = "application/json"

    def do_GET(self):
        self._respond()

    def do_POST(self):
        self._respond()

    def _respond(self):
        self.send_response(self.status_code)
        self.send_header("Content-Type", self.content_type)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *_):  # silence default access log
        pass


def _start_mock_server(handler_class=_MockHandler):
    """Start a throwaway HTTP server on localhost:0, return (server, url)."""
    server = HTTPServer(("127.0.0.1", 0), handler_class)
    port   = server.server_address[1]
    t      = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, f"http://127.0.0.1:{port}"


# ═══════════════════════════════════════════════════════════════════════════════
# 1. news_aggregator.py — NewsCache SQLite connection safety
# ═══════════════════════════════════════════════════════════════════════════════

class TestNewsCacheSQLiteLeaks(unittest.TestCase):
    """SQLite connections in NewsCache must be closed even when operations fail."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name

    def tearDown(self):
        os.unlink(self.db_path)

    def test_init_db_closes_on_success(self):
        from news_aggregator import NewsCache
        cache = NewsCache(self.db_path, ttl_minutes=5)
        # If the connection leaked we'd see it in lsof; here just assert the DB
        # is usable (i.e. not locked by a dangling connection).
        conn = sqlite3.connect(self.db_path)
        conn.execute("SELECT count(*) FROM news_cache")
        conn.close()

    def test_get_closes_on_missing_row(self):
        from news_aggregator import NewsCache
        cache = NewsCache(self.db_path)
        result = cache.get("nonexistent_hash")
        self.assertIsNone(result)
        # DB should be cleanly accessible (no WAL lock)
        conn = sqlite3.connect(self.db_path, timeout=1)
        conn.close()

    def test_set_and_get_roundtrip(self):
        from news_aggregator import NewsCache
        cache = NewsCache(self.db_path)
        cache.set("abc123", {"title": "test", "url": "http://x"})
        result = cache.get("abc123")
        self.assertEqual(result["title"], "test")

    def test_cleanup_expired_leaves_db_accessible(self):
        from news_aggregator import NewsCache
        cache = NewsCache(self.db_path, ttl_minutes=0)
        cache.set("stale", {"title": "old"})
        cache.cleanup_expired()
        conn = sqlite3.connect(self.db_path, timeout=1)
        conn.close()

    @_skip_if_fd_count_unavailable
    def test_repeated_operations_no_fd_growth(self):
        """100 cache operations must not grow the open-FD count."""
        from news_aggregator import NewsCache
        cache = NewsCache(self.db_path)
        # warm-up
        for i in range(5):
            cache.set(f"h{i}", {"title": str(i)})
        baseline = _open_fd_count()
        for i in range(100):
            cache.set(f"key{i}", {"title": str(i)})
            cache.get(f"key{i}")
        final = _open_fd_count()
        self.assertLessEqual(final, baseline + 3,
            f"FD count grew from {baseline} to {final} — possible SQLite leak")


# ═══════════════════════════════════════════════════════════════════════════════
# 2. monitoring.py — HTTP response always closed
# ═══════════════════════════════════════════════════════════════════════════════

class TestMonitoringResponseClosure(unittest.TestCase):
    """monitoring.py fetch methods must close responses on both success and error."""

    def _make_mock_response(self, status=200, body=None):
        resp = MagicMock()
        resp.status_code = status
        resp.ok = (status < 400)
        body = body or b'{"chart": {"result": [{"meta": {"regularMarketPrice": 5000, "chartPreviousClose": 4900}}]}}'
        resp.json.return_value = json.loads(body)
        resp.text = body.decode() if isinstance(body, bytes) else body
        resp.close = MagicMock()
        return resp

    @patch("monitoring.requests.get")
    def test_fetch_vix_closes_on_success(self, mock_get):
        from monitoring import SignalMonitor
        body = b'{"chart": {"result": [{"meta": {"regularMarketPrice": 18.5}}]}}'
        mock_get.return_value = self._make_mock_response(200, body)
        monitor = SignalMonitor(":memory:")
        monitor.fetch_vix()
        mock_get.return_value.close.assert_called_once()

    @patch("monitoring.requests.get")
    def test_fetch_vix_closes_on_error_status(self, mock_get):
        from monitoring import SignalMonitor
        resp = MagicMock()
        resp.status_code = 500
        resp.ok = False
        resp.text = "server error"
        resp.close = MagicMock()
        mock_get.return_value = resp
        monitor = SignalMonitor(":memory:")
        monitor.fetch_vix()
        resp.close.assert_called_once()

    @patch("monitoring.requests.get")
    def test_fetch_market_prices_closes_on_success(self, mock_get):
        from monitoring import SignalMonitor
        mock_get.return_value = self._make_mock_response(200)
        monitor = SignalMonitor(":memory:")
        monitor.fetch_market_prices()
        mock_get.return_value.close.assert_called_once()

    @patch("monitoring.requests.get")
    def test_fetch_bond_yield_closes_on_success(self, mock_get):
        from monitoring import SignalMonitor
        body = b'{"observations": [{"value": "4.25", "date": "2026-01-01"}]}'
        mock_get.return_value = self._make_mock_response(200, body)
        with patch("monitoring._load_fred_api_key", return_value="FAKE"):
            monitor = SignalMonitor(":memory:")
            monitor.fetch_bond_yield()
        mock_get.return_value.close.assert_called_once()

    @patch("monitoring.requests.get")
    def test_fetch_bond_yield_closes_on_json_exception(self, mock_get):
        """Even if json() raises, the socket must be closed."""
        from monitoring import SignalMonitor
        resp = self._make_mock_response(200)
        resp.json.side_effect = ValueError("bad json")
        mock_get.return_value = resp
        with patch("monitoring._load_fred_api_key", return_value="FAKE"):
            monitor = SignalMonitor(":memory:")
            monitor.fetch_bond_yield()
        resp.close.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════════
# 3. fred_macro.py — _safe_get response closure + SQLite
# ═══════════════════════════════════════════════════════════════════════════════

class TestFredMacroLeaks(unittest.TestCase):

    @patch("fred_macro.requests.get")
    def test_safe_get_closes_on_200(self, mock_get):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"observations": []}
        resp.close = MagicMock()
        mock_get.return_value = resp
        import fred_macro
        fred_macro._safe_get("http://fake", {})
        resp.close.assert_called_once()

    @patch("fred_macro.requests.get")
    def test_safe_get_closes_on_non_200(self, mock_get):
        resp = MagicMock()
        resp.status_code = 429
        resp.close = MagicMock()
        mock_get.return_value = resp
        import fred_macro
        fred_macro._safe_get("http://fake", {})
        resp.close.assert_called_once()

    @patch("fred_macro.requests.get")
    def test_safe_get_closes_on_exception(self, mock_get):
        import requests as _req
        mock_get.side_effect = _req.exceptions.ConnectionError("refused")
        import fred_macro
        result = fred_macro._safe_get("http://fake", {})
        self.assertIsNone(result)  # should return None gracefully

    def test_get_latest_from_db_closes_on_bad_db(self):
        """get_latest_from_db must not leak conn when DB doesn't have the table."""
        import fred_macro
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            tracker = fred_macro.FREDMacroTracker(api_key="FAKE", db_path=db_path)
            result = tracker.get_latest_from_db()
            self.assertIsNone(result)
            # DB still accessible (no lingering lock)
            conn = sqlite3.connect(db_path, timeout=1)
            conn.close()
        finally:
            os.unlink(db_path)


# ═══════════════════════════════════════════════════════════════════════════════
# 4. paper_trading.py — AlpacaBroker session reuse + response closure
# ═══════════════════════════════════════════════════════════════════════════════

class TestAlpacaBrokerLeaks(unittest.TestCase):

    def _mock_response(self, status=200, body=None):
        resp = MagicMock()
        resp.ok = (status < 400)
        resp.status_code = status
        body = body or {"id": "abc123", "status": "accepted"}
        resp.json.return_value = body
        resp.text = json.dumps(body)
        resp.close = MagicMock()
        return resp

    def test_module_session_exists(self):
        """paper_trading must expose a module-level _BROKER_SESSION."""
        import paper_trading
        import requests
        self.assertIsInstance(paper_trading._BROKER_SESSION, requests.Session)

    @patch("paper_trading._BROKER_SESSION")
    def test_place_order_closes_response_on_success(self, mock_session):
        resp = self._mock_response(200)
        mock_session.post.return_value = resp
        from paper_trading import AlpacaBroker
        broker = AlpacaBroker.__new__(AlpacaBroker)
        broker.api_key    = "K"
        broker.secret_key = "S"
        broker.base_url   = "http://fake"
        broker._configured = True
        broker.place_order("AAPL", 1, "buy")
        resp.close.assert_called_once()

    @patch("paper_trading._BROKER_SESSION")
    def test_place_order_closes_response_on_failure(self, mock_session):
        resp = self._mock_response(422, {"message": "insufficient funds"})
        mock_session.post.return_value = resp
        from paper_trading import AlpacaBroker
        broker = AlpacaBroker.__new__(AlpacaBroker)
        broker.api_key    = "K"
        broker.secret_key = "S"
        broker.base_url   = "http://fake"
        broker._configured = True
        result = broker.place_order("AAPL", 1, "buy")
        self.assertFalse(result["ok"])
        resp.close.assert_called_once()

    @patch("paper_trading._BROKER_SESSION")
    def test_get_account_closes_response(self, mock_session):
        resp = self._mock_response(200, {"equity": "50000"})
        mock_session.get.return_value = resp
        from paper_trading import AlpacaBroker
        broker = AlpacaBroker.__new__(AlpacaBroker)
        broker.api_key    = "K"
        broker.secret_key = "S"
        broker.base_url   = "http://fake"
        broker._configured = True
        broker.get_account()
        resp.close.assert_called_once()

    @patch("paper_trading._BROKER_SESSION")
    def test_get_positions_closes_response(self, mock_session):
        resp = self._mock_response(200, [])
        mock_session.get.return_value = resp
        from paper_trading import AlpacaBroker
        broker = AlpacaBroker.__new__(AlpacaBroker)
        broker.api_key    = "K"
        broker.secret_key = "S"
        broker.base_url   = "http://fake"
        broker._configured = True
        broker.get_positions()
        resp.close.assert_called_once()

    @_skip_if_fd_count_unavailable
    def test_rapid_orders_no_fd_growth(self):
        """50 simulated order calls must not grow the open-FD count."""
        server, url = _start_mock_server()
        try:
            import paper_trading
            # Point broker at local mock server
            broker = paper_trading.AlpacaBroker.__new__(paper_trading.AlpacaBroker)
            broker.api_key    = "K"
            broker.secret_key = "S"
            broker.base_url   = url
            broker._configured = True

            # warm-up
            for _ in range(3):
                broker.get_account()
            baseline = _open_fd_count()
            for _ in range(50):
                broker.get_account()
            final = _open_fd_count()
            self.assertLessEqual(final, baseline + 5,
                f"FD count grew from {baseline} to {final} after 50 broker calls")
        finally:
            server.shutdown()


# ═══════════════════════════════════════════════════════════════════════════════
# 5. gemini_client.py — REST response always closed
# ═══════════════════════════════════════════════════════════════════════════════

class TestGeminiClientResponseClosure(unittest.TestCase):

    def _make_resp(self, status=200, body=None):
        resp = MagicMock()
        resp.ok = (status < 400)
        resp.status_code = status
        body = body or {
            "candidates": [{"content": {"parts": [{"text": "hello"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "thoughtsTokenCount": 0}
        }
        resp.json.return_value = body
        resp.text = json.dumps(body)
        resp.raise_for_status = MagicMock()
        if status >= 400:
            import requests
            resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=resp)
        resp.close = MagicMock()
        return resp

    @patch("gemini_client.requests.post")
    def test_rest_closes_on_success(self, mock_post):
        """_call_via_api must close the response after a successful REST call."""
        resp = self._make_resp(200)
        mock_post.return_value = resp

        import gemini_client
        # Patch _get_api_key so it returns a fake key (skip real auth)
        with patch("gemini_client._get_api_key", return_value="FAKE_KEY"), \
             patch("gemini_client._google_search_grounding_enabled", return_value=False):
            try:
                gemini_client._call_via_api(
                    prompt="hello",
                    model_id="gemini-3-flash-preview",
                    temperature=0.3,
                    thinking_budget=0,
                    max_output_tokens=1000,
                )
            except Exception:
                pass
        resp.close.assert_called_once()

    @patch("gemini_client.requests.post")
    def test_rest_closes_on_http_error(self, mock_post):
        """_call_via_api must close the response even when the server returns 429."""
        resp = self._make_resp(429)
        mock_post.return_value = resp

        import gemini_client
        with patch("gemini_client._get_api_key", return_value="FAKE_KEY"), \
             patch("gemini_client._google_search_grounding_enabled", return_value=False):
            try:
                gemini_client._call_via_api(
                    prompt="hello",
                    model_id="gemini-3-flash-preview",
                    temperature=0.3,
                    thinking_budget=0,
                    max_output_tokens=1000,
                )
            except Exception:
                pass
        resp.close.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════════
# 6. gemini_analyzer.py — save_analysis_to_db SQLite closure on exception
# ═══════════════════════════════════════════════════════════════════════════════

class TestGeminiAnalyzerSQLiteLeaks(unittest.TestCase):

    def test_save_analysis_closes_on_bad_table(self):
        """save_analysis_to_db must close conn even when the table doesn't exist."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            from gemini_analyzer import GeminiAnalyzer
            analyzer = GeminiAnalyzer()
            result = analyzer.save_analysis_to_db(
                db_path=db_path,
                news_signal_id=1,
                analysis={"model": "test", "recommended_action": "WAIT",
                           "narrative_coherence": 0.5, "hidden_risks": [],
                           "contrarian_signals": "", "market_context": "",
                           "confidence_in_signal": 0.5, "reasoning": "",
                           "input_tokens": 0, "output_tokens": 0, "data_gaps": []},
                trigger_type="flash",
            )
            # No table → should return None, not crash
            self.assertIsNone(result)
            # DB should still be connectable (no dangling lock)
            conn = sqlite3.connect(db_path, timeout=1)
            conn.close()
        finally:
            os.unlink(db_path)

    @_skip_if_fd_count_unavailable
    def test_repeated_saves_no_fd_growth(self):
        """Many failed save attempts must not accumulate open FDs."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            from gemini_analyzer import GeminiAnalyzer
            analyzer = GeminiAnalyzer()
            analysis = {"model": "test", "recommended_action": "WAIT",
                        "narrative_coherence": 0.5, "hidden_risks": [],
                        "contrarian_signals": "", "market_context": "",
                        "confidence_in_signal": 0.5, "reasoning": "",
                        "input_tokens": 0, "output_tokens": 0, "data_gaps": []}
            # warm-up
            for _ in range(5):
                analyzer.save_analysis_to_db(db_path, 1, analysis, "flash")
            baseline = _open_fd_count()
            for _ in range(50):
                analyzer.save_analysis_to_db(db_path, 1, analysis, "flash")
            final = _open_fd_count()
            self.assertLessEqual(final, baseline + 3,
                f"FD count grew from {baseline} to {final} — possible SQLite leak")
        finally:
            os.unlink(db_path)


# ═══════════════════════════════════════════════════════════════════════════════
# 7. 429 / backoff — grok_client already has backoff; test response closure
# ═══════════════════════════════════════════════════════════════════════════════

class TestGrokClient429Handling(unittest.TestCase):
    """grok_client.py already implements exponential backoff for 429s.
    Verify that responses are still closed on every 429 attempt."""

    def _make_resp(self, status):
        resp = MagicMock()
        resp.status_code = status
        resp.ok = (status < 400)
        resp.json.return_value = {}
        resp.text = ""
        resp.close = MagicMock()
        return resp

    @patch("grok_client._SESSION")
    def test_429_responses_are_closed(self, mock_session):
        """Each 429 response object must be closed before the next retry."""
        import grok_client
        # Simulate 2× 429 then success
        r429a = self._make_resp(429)
        r429b = self._make_resp(429)
        r_ok  = self._make_resp(200)
        r_ok.json.return_value = {
            "choices": [{"message": {"content": "answer"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3}
        }
        mock_session.post.side_effect = [r429a, r429b, r_ok]

        with patch("grok_client.time.sleep"):  # don't actually sleep in tests
            client = grok_client.GrokClient.__new__(grok_client.GrokClient)
            client.api_key       = "FAKE"
            client.base_url      = "http://fake"
            client.model         = "grok-4-1-fast-reasoning"
            client.default_model = "grok-4-1-fast-reasoning"
            try:
                # call() has no max_retries param — retries live in _post_json_with_backoff
                client.call("hello")
            except Exception:
                pass

        # _post_json_with_backoff must close each 429 response before the next attempt,
        # and call() must close the final response after reading it.
        r429a.close.assert_called()
        r429b.close.assert_called()
        r_ok.close.assert_called()


# ─── entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
