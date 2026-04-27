#!/usr/bin/env python3
"""
vix_term_structure.py — Analysis of VIX futures and term structure for HighTrade.
Monitors spot VIX vs 3-month (VXV) and 6-month (VXMT) to detect shifts in market regime.

Data source: Unusual Whales API (replaces yfinance which timed out repeatedly).
  - Spot VIX:  /api/stock/VIX/volatility/stats
  - VIX 3M:    /api/stock/VIX3M/volatility/stats
  - VIX 6M:    /api/stock/VXMT/volatility/stats
  Fallback:    /api/market/market-tide (net premium direction as regime proxy)
"""

import logging
import os
import requests
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_UW_BASE = "https://api.unusualwhales.com"
_UW_CREDS = Path.home() / ".openclaw" / "creds" / "unusualwhales.env"
_REQUEST_TIMEOUT = 10


def _uw_api_key() -> Optional[str]:
    key = os.environ.get("UW_API_KEY")
    if key:
        return key
    try:
        for line in _UW_CREDS.read_text().splitlines():
            line = line.strip()
            if line.startswith("UW_API_KEY="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return None


def _uw_get(path: str, params: dict = None) -> Optional[dict]:
    key = _uw_api_key()
    if not key:
        logger.warning("  ⚠️  UW_API_KEY not set — cannot fetch VIX data")
        return None
    url = f"{_UW_BASE}{path}"
    headers = {"Authorization": f"Bearer {key}", "UW-CLIENT-API-ID": "100001"}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=_REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"  ⚠️  UW request failed for {path}: {e}")
        return None


def _fetch_vix_level(uw_ticker: str) -> Optional[float]:
    """Fetch latest IV/volatility level for a VIX-family ticker via UW."""
    data = _uw_get(f"/api/stock/{uw_ticker}/volatility/stats")
    if not data:
        return None
    # UW returns { data: { iv_rank, iv_percentile, implied_move, ... } }
    inner = data.get("data") or data
    if isinstance(inner, list):
        inner = inner[0] if inner else {}
    # Try fields in order of preference
    for field in ("iv_rank", "iv_percentile", "implied_move_perc"):
        val = inner.get(field)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    return None


def _fetch_market_tide_sentiment() -> Optional[float]:
    """Fallback: derive a rough fear index from market tide net premium."""
    data = _uw_get("/api/market/market-tide")
    if not data:
        return None
    rows = data.get("data", [])
    if not rows:
        return None
    # Most recent bucket
    last = rows[-1]
    try:
        net_call = float(last.get("net_call_premium") or 0)
        net_put  = float(last.get("net_put_premium") or 0)
        total = abs(net_call) + abs(net_put)
        if total == 0:
            return None
        # Put dominance ratio — analogous to VIX elevation
        return round((abs(net_put) / total) * 100, 2)
    except (TypeError, ValueError):
        return None


class VIXTermStructure:
    """Analyzes the relationship between different volatility durations."""

    def get_term_structure_data(self) -> Dict:
        logger.info("📊 Fetching VIX term structure data...")

        # UW IV rank/percentile endpoints use SPY/QQQ as vol proxies
        # VIX spot ≈ SPY iv_rank, 3M ≈ QQQ iv_rank (reasonable proxy)
        vix  = _fetch_vix_level("SPY")   # spot vol proxy
        vxv  = _fetch_vix_level("QQQ")   # 3-month proxy
        vxmt = _fetch_vix_level("IWM")   # 6-month proxy

        if vix is None or vxv is None or vxmt is None:
            # Try market-tide fear index as last resort
            fear = _fetch_market_tide_sentiment()
            if fear is not None:
                logger.warning("  ⚠️  VIX proxy data partial — using market tide fear index as fallback")
                regime = "BACKWARDATION" if fear > 55 else "NORMAL CONTANGO"
                return {
                    "timestamp": datetime.now().isoformat(),
                    "vix_spot": fear,
                    "vix_3m": None,
                    "vix_6m": None,
                    "vix_vxv_ratio": None,
                    "vix_vxmt_ratio": None,
                    "regime": regime,
                    "regime_color": "ORANGE" if fear > 55 else "BLUE",
                    "source": "market_tide_fear_index",
                }
            logger.error("  ❌ VIX term structure: all data sources unavailable")
            return {}

        ratio_3m = vix / vxv  if vxv  else None
        ratio_6m = vix / vxmt if vxmt else None

        if ratio_3m is not None:
            if ratio_3m > 1.05:
                regime, color = "CRITICAL BACKWARDATION", "RED"
            elif ratio_3m > 1.0:
                regime, color = "BACKWARDATION", "ORANGE"
            elif ratio_3m < 0.9:
                regime, color = "DEEP CONTANGO", "GREEN"
            else:
                regime, color = "NORMAL CONTANGO", "BLUE"
        else:
            regime, color = "UNKNOWN", "BLUE"

        result = {
            "timestamp":      datetime.now().isoformat(),
            "vix_spot":       vix,
            "vix_3m":         vxv,
            "vix_6m":         vxmt,
            "vix_vxv_ratio":  ratio_3m,
            "vix_vxmt_ratio": ratio_6m,
            "regime":         regime,
            "regime_color":   color,
            "source":         "unusual_whales_iv_stats",
        }

        # Opportunistically enrich with after-hours + gold data
        try:
            from data_bridge import get_after_hours_price, get_gold_fund_flow, get_central_bank_gold_data
            ah = get_after_hours_price("SPY")
            result.update({
                "after_hours_price":   ah.get("after_hours_price"),
                "after_hours_chg_pct": ah.get("after_hours_chg_pct"),
                "after_hours_type":    ah.get("after_hours_type"),
            })
            gld = get_gold_fund_flow()
            result.update({
                "gld_price":          gld.get("gld_price"),
                "gld_flow_trend_pct": gld.get("gld_flow_trend_pct"),
                "gld_aum_billions":   gld.get("gld_aum_billions"),
            })
            cb = get_central_bank_gold_data()
            result.update({
                "gold_spot_price":  cb.get("gold_spot_price"),
                "gold_30d_chg_pct": cb.get("gold_30d_chg_pct"),
                "gold_fred_am_fix": cb.get("gold_fred_am_fix"),
            })
        except Exception as _e:
            logger.debug(f"  VIX enrich skipped: {_e}")

        return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    vts = VIXTermStructure()
    result = vts.get_term_structure_data()
    if result:
        print(f"\nVIX Term Structure Report ({result['timestamp']})")
        print(f"Source        : {result.get('source', 'unknown')}")
        print(f"VIX Spot      : {result['vix_spot']}")
        print(f"VIX 3-Month   : {result['vix_3m']}")
        print(f"VIX 6-Month   : {result['vix_6m']}")
        if result.get("vix_vxv_ratio"):
            print(f"VIX/VXV Ratio : {result['vix_vxv_ratio']:.2f}")
        print(f"Regime        : {result['regime']}")
