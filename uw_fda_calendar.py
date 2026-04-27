#!/usr/bin/env python3
"""
uw_fda_calendar.py — Unusual Whales FDA calendar feed for HighTrade.
Provides upcoming FDA decision events for biotech catalyst detection.
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_UW_BASE = "https://api.unusualwhales.com"
_UW_API_KEY: Optional[str] = None
_UW_KEY_LOGGED = False


def _load_uw_key() -> Optional[str]:
    global _UW_API_KEY, _UW_KEY_LOGGED
    if _UW_API_KEY is not None:
        return _UW_API_KEY
    creds_path = Path.home() / ".openclaw" / "creds" / "unusualwhales.env"
    try:
        for line in creds_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("UW_API_KEY"):
                _UW_API_KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
                return _UW_API_KEY
    except Exception:
        pass
    if not _UW_KEY_LOGGED:
        logger.warning("UW FDA calendar: unusualwhales.env not found or missing UW_API_KEY — FDA calendar disabled")
        _UW_KEY_LOGGED = True
    return None


class FDACalendar:
    """Fetches and filters upcoming FDA decision events from Unusual Whales."""

    def _uw_get(self, path: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """Authenticated GET to Unusual Whales API. Returns parsed JSON or None."""
        key = _load_uw_key()
        if not key:
            return None
        import requests
        url = f"{_UW_BASE}{path}"
        headers = {
            "Authorization": f"Bearer {key}",
            "UW-CLIENT-API-ID": "100001",
        }
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.debug(f"UW FDA calendar request failed [{path}]: {e}")
            return None

    def get_upcoming_events(self) -> list:
        """
        GET /api/market/fda-calendar
        Returns list of upcoming FDA decision events.
        Each event is a dict with at minimum: ticker, date, drug_name, catalyst_type.
        Falls back to empty list on failure.
        """
        try:
            data = self._uw_get("/api/market/fda-calendar")
            if not data:
                return []

            # UW may wrap in data/results key
            events = data.get("data") or data.get("results") or data.get("events") or data
            if isinstance(events, list):
                return events
            return []
        except Exception as e:
            logger.debug(f"get_upcoming_events failed: {e}")
            return []

    def get_events_this_week(self) -> list:
        """
        Filters upcoming FDA events to those occurring within the next 7 days.
        Returns list of event dicts.
        """
        try:
            events = self.get_upcoming_events()
            if not events:
                return []

            now = datetime.now()
            cutoff = now + timedelta(days=7)
            result = []
            for event in events:
                event_date = self._parse_event_date(event)
                if event_date and now.date() <= event_date.date() <= cutoff.date():
                    result.append(event)
            return result
        except Exception as e:
            logger.debug(f"get_events_this_week failed: {e}")
            return []

    def is_biotech_catalyst_window(self, ticker: str) -> bool:
        """
        Returns True if the given ticker has an FDA event within the next 14 days.
        """
        try:
            ticker = ticker.upper().strip()
            events = self.get_upcoming_events()
            if not events:
                return False

            now = datetime.now()
            cutoff = now + timedelta(days=14)
            for event in events:
                event_ticker = str(event.get("ticker") or event.get("symbol") or "").upper().strip()
                if event_ticker != ticker:
                    continue
                event_date = self._parse_event_date(event)
                if event_date and now.date() <= event_date.date() <= cutoff.date():
                    return True
        except Exception as e:
            logger.debug(f"is_biotech_catalyst_window({ticker}) failed: {e}")
        return False

    def format_for_prompt(self, events: list) -> str:
        """
        Formats a list of FDA event dicts as clean text for injection into LLM prompts.
        Returns a human-readable summary string.
        """
        if not events:
            return "No FDA calendar events found."

        lines = []
        for event in events:
            ticker = str(event.get("ticker") or event.get("symbol") or "UNKNOWN").upper()
            drug = str(event.get("drug_name") or event.get("drug") or event.get("name") or "")
            catalyst = str(event.get("catalyst_type") or event.get("type") or event.get("event_type") or "PDUFA")
            date_str = str(event.get("date") or event.get("event_date") or event.get("decision_date") or "TBD")
            company = str(event.get("company") or event.get("company_name") or "")

            parts = [f"{ticker}"]
            if company:
                parts.append(f"({company})")
            parts.append(f"— {catalyst}")
            if drug:
                parts.append(f"for {drug}")
            parts.append(f"on {date_str}")
            lines.append(" ".join(parts))

        return "\n".join(lines)

    def _parse_event_date(self, event: dict) -> Optional[datetime]:
        """Parse event date from various possible field names and formats."""
        date_val = event.get("date") or event.get("event_date") or event.get("decision_date")
        if not date_val:
            return None
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%m/%d/%Y"):
            try:
                return datetime.strptime(str(date_val)[:len(fmt)], fmt)
            except ValueError:
                continue
        return None
