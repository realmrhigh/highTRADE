#!/usr/bin/env python3
"""
uw_seasonality.py — Unusual Whales seasonality data feed for HighTrade.
Provides historical monthly return averages and sector bias for confidence scoring.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

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
        logger.warning("UW seasonality: unusualwhales.env not found or missing UW_API_KEY — seasonality disabled")
        _UW_KEY_LOGGED = True
    return None


class SeasonalityAdvisor:
    """Provides historical seasonality data from Unusual Whales for market and individual tickers."""

    # Rough sector outperformance by month based on historical patterns.
    # Used as fallback when API data is insufficient.
    _SECTOR_BIAS_MAP = {
        1:  "Technology, Consumer Discretionary",   # Jan effect
        2:  "Healthcare, Financials",
        3:  "Energy, Industrials",
        4:  "Technology, Consumer Discretionary",   # Spring rally
        5:  "Defensive (Utilities, Consumer Staples)",  # Sell in May caution
        6:  "Energy, Materials",
        7:  "Technology, Consumer Discretionary",   # Summer rally
        8:  "Defensive (Utilities, Consumer Staples)",
        9:  "Cash/Defensive (weakest month historically)",
        10: "Energy, Financials",                   # Q4 setup
        11: "Technology, Consumer Discretionary",   # Santa rally begins
        12: "Technology, Consumer Discretionary, Small Caps",
    }

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
            logger.debug(f"UW seasonality request failed [{path}]: {e}")
            return None

    def get_market_seasonality(self) -> dict:
        """
        GET /api/seasonality/market
        Returns monthly return averages for the broad market.
        Falls back to empty dict on failure.
        """
        try:
            data = self._uw_get("/api/seasonality/market")
            if data:
                return data
        except Exception as e:
            logger.debug(f"get_market_seasonality failed: {e}")
        return {}

    def get_ticker_seasonality(self, ticker: str) -> dict:
        """
        GET /api/seasonality/{ticker}/monthly
        Returns average return per month for the given ticker.
        Falls back to empty dict on failure.
        """
        try:
            ticker = ticker.upper().strip()
            data = self._uw_get(f"/api/seasonality/{ticker}/monthly")
            if data:
                return data
        except Exception as e:
            logger.debug(f"get_ticker_seasonality({ticker}) failed: {e}")
        return {}

    def get_current_month_score(self) -> float:
        """
        Returns the current month's historical win rate (0-100) from market seasonality data.
        Win rate represents the percentage of years where this month produced a positive return.
        Falls back to 50.0 (neutral) on any failure.
        """
        try:
            data = self.get_market_seasonality()
            if not data:
                return 50.0

            current_month = datetime.now().month

            # UW API may return data keyed by month number or month name.
            # Try common response shapes.
            monthly = data.get("monthly") or data.get("data") or data.get("months") or data

            if isinstance(monthly, list):
                for entry in monthly:
                    month_val = entry.get("month") or entry.get("month_num") or entry.get("month_number")
                    try:
                        if int(month_val) == current_month:
                            win_rate = entry.get("win_rate") or entry.get("positive_years_pct") or entry.get("positive_rate")
                            if win_rate is not None:
                                return float(win_rate)
                    except (TypeError, ValueError):
                        continue
            elif isinstance(monthly, dict):
                month_key = str(current_month)
                entry = monthly.get(month_key) or monthly.get(current_month)
                if isinstance(entry, dict):
                    win_rate = entry.get("win_rate") or entry.get("positive_years_pct") or entry.get("positive_rate")
                    if win_rate is not None:
                        return float(win_rate)
                elif isinstance(entry, (int, float)):
                    # Some endpoints return raw win rate directly
                    return float(entry)

        except Exception as e:
            logger.debug(f"get_current_month_score failed: {e}")

        return 50.0

    def get_sector_bias(self) -> str:
        """
        Returns which sectors historically outperform in the current month.
        Based on market seasonality data with fallback to built-in map.
        """
        try:
            current_month = datetime.now().month
            data = self.get_market_seasonality()

            if data:
                # Try to extract sector bias from API response
                monthly = data.get("monthly") or data.get("data") or data.get("months") or data
                if isinstance(monthly, list):
                    for entry in monthly:
                        try:
                            month_val = entry.get("month") or entry.get("month_num") or entry.get("month_number")
                            if int(month_val) == current_month:
                                sector = entry.get("top_sectors") or entry.get("sector_bias") or entry.get("sectors")
                                if sector:
                                    return str(sector)
                        except (TypeError, ValueError):
                            continue

        except Exception as e:
            logger.debug(f"get_sector_bias failed: {e}")

        # Fallback to built-in map
        current_month = datetime.now().month
        return self._SECTOR_BIAS_MAP.get(current_month, "No strong sector bias data available")
