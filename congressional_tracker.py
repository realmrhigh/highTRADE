#!/usr/bin/env python3
"""
Congressional Trading Tracker
Fetches House/Senate stock disclosures via Unusual Whales API
and SEC EDGAR Form 4 insider trading filings.

Data sources:
  - Unusual Whales: https://api.unusualwhales.com/api/congress/recent-trades
  - Unusual Whales: https://api.unusualwhales.com/api/politician-portfolios/recent_trades
  - SEC EDGAR: https://efts.sec.gov/LATEST/search-index?q=%22Form+4%22&dateRange=custom

Signals generated:
  - Cluster buys: 3+ politicians buy same stock within 30 days → BULLISH
  - Committee-relevant buys: Armed Services member buys defense stock → HIGH ALPHA
  - Large insider purchase: Form 4 non-routine >$100K → notable
  - Politician sells before drop (retrospective pattern) → flagged for model learning
"""

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

from trading_db import get_sqlite_conn

logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent.resolve()
DB_PATH = SCRIPT_DIR / 'trading_data' / 'trading_history.db'

# Unusual Whales endpoints
UW_RECENT_TRADES_URL = "https://api.unusualwhales.com/api/congress/recent-trades"
UW_PORTFOLIO_TRADES_URL = "https://api.unusualwhales.com/api/politician-portfolios/recent_trades"
UW_CLIENT_ID = "100001"
UW_CREDS_FILE = Path.home() / ".openclaw" / "creds" / "unusualwhales.env"

SEC_EDGAR_SEARCH = "https://efts.sec.gov/LATEST/search-index"


def _load_uw_api_key() -> Optional[str]:
    """Load UW_API_KEY from ~/.openclaw/creds/unusualwhales.env"""
    try:
        for line in UW_CREDS_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith('#') or '=' not in line:
                continue
            key, _, val = line.partition('=')
            if key.strip() == 'UW_API_KEY':
                return val.strip().strip('"').strip("'")
    except Exception as e:
        logger.warning(f"Could not load UW API key from {UW_CREDS_FILE}: {e}")
    return None

# Committee power map: which committee members have early intel on which sectors
COMMITTEE_INTEL_MAP = {
    'Armed Services': ['LMT', 'RTX', 'NOC', 'GD', 'BA', 'L3H', 'HII', 'LDOS', 'SAIC', 'CACI'],
    'Intelligence': ['PLTR', 'CACI', 'SAIC', 'LDOS', 'BOOZ'],
    'Banking': ['JPM', 'BAC', 'WFC', 'GS', 'MS', 'C', 'USB', 'PNC'],
    'Finance': ['JPM', 'BAC', 'V', 'MA', 'PYPL', 'AXP', 'COF'],
    'Energy': ['XOM', 'CVX', 'COP', 'PXD', 'OXY', 'SLB', 'HAL', 'MPC', 'PSX', 'VLO'],
    'Commerce': ['AMZN', 'GOOGL', 'META', 'MSFT', 'AAPL', 'NFLX', 'UBER', 'LYFT'],
    'Health': ['UNH', 'CVS', 'CI', 'HUM', 'MCK', 'ABC', 'CAH', 'LLY', 'PFE', 'MRK'],
    'Agriculture': ['ADM', 'BG', 'CTVA', 'FMC', 'MOS', 'NTR', 'DE', 'AGCO'],
    'Judiciary': ['GOOGL', 'META', 'AMZN', 'AAPL', 'MSFT'],  # antitrust
}

# Minimum trade size to track (USD)
MIN_TRADE_SIZE = 15000
CLUSTER_WINDOW_DAYS = 30
CLUSTER_MIN_COUNT = 3


def _safe_get(url: str, params: dict = None, timeout: int = 15) -> Optional[dict]:
    """Safe HTTP GET with error handling (unauthenticated, for SEC EDGAR)"""
    try:
        headers = {
            'User-Agent': 'HighTrade Research Bot (research purposes)',
            'Accept': 'application/json'
        }
        resp = requests.get(url, params=params, headers=headers, timeout=timeout)
        if resp.status_code == 200:
            return resp.json()
        else:
            logger.debug(f"HTTP {resp.status_code} from {url}")
            return None
    except Exception as e:
        logger.debug(f"Request failed for {url}: {e}")
        return None


def _uw_get(url: str, api_key: str, params: dict = None, timeout: int = 15) -> Optional[dict]:
    """Authenticated GET to Unusual Whales API"""
    try:
        headers = {
            'Authorization': f'Bearer {api_key}',
            'UW-CLIENT-API-ID': UW_CLIENT_ID,
            'Accept': 'application/json',
        }
        resp = requests.get(url, params=params, headers=headers, timeout=timeout)
        if resp.status_code == 200:
            return resp.json()
        else:
            logger.warning(f"UW API HTTP {resp.status_code} from {url}: {resp.text[:200]}")
            return None
    except Exception as e:
        logger.warning(f"UW API request failed for {url}: {e}")
        return None


class CongressionalTracker:
    """Tracks congressional stock trades and generates alpha signals"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or str(DB_PATH)
        self._api_key: Optional[str] = None
        self._last_fetch_time = None
        self._cache_minutes = 60  # Re-fetch at most once per hour

    def _get_api_key(self) -> Optional[str]:
        if self._api_key is None:
            self._api_key = _load_uw_api_key()
        return self._api_key

    def _parse_uw_trade(self, item: dict) -> Optional[Dict]:
        """
        Map an Unusual Whales trade object to our internal trade dict.
        UW fields: politician_name, full_name, party, chamber, ticker, asset_ticker,
                   transaction_type, type, amount, range, disclosure_date, traded_at,
                   transaction_date, asset_description, state, district, committees
        """
        try:
            ticker = (
                item.get('ticker') or item.get('asset_ticker') or ''
            ).strip().upper()
            if not ticker or ticker in ('N/A', '--', ''):
                return None

            tx_type = (
                item.get('transaction_type') or item.get('type') or ''
            ).lower()
            if 'purchase' in tx_type or 'buy' in tx_type:
                direction = 'buy'
            elif 'sale' in tx_type or 'sell' in tx_type:
                direction = 'sell'
            else:
                direction = 'unknown'

            # Amount: UW may return a numeric value or a range string
            amount_raw = item.get('amount') or item.get('range') or item.get('value') or ''
            if isinstance(amount_raw, (int, float)):
                amount = float(amount_raw)
            else:
                amount = self._parse_amount_range(str(amount_raw))

            # Dates
            disclosure_raw = (
                item.get('disclosure_date') or item.get('filed_at') or ''
            )[:10]
            tx_raw = (
                item.get('transaction_date') or item.get('traded_at') or
                item.get('tx_date') or disclosure_raw
            )[:10]

            if not disclosure_raw:
                return None

            # Politician name and party
            name = (
                item.get('politician_name') or item.get('full_name') or
                item.get('name') or 'Unknown'
            )
            party = (item.get('party') or '?')[:1].upper() if item.get('party') else '?'

            chamber = (item.get('chamber') or '').lower()
            source = 'house' if 'house' in chamber or 'rep' in chamber else \
                     'senate' if 'senate' in chamber or 'sen' in chamber else \
                     chamber or 'congress'

            committees = item.get('committees') or []
            committee_hint = ', '.join(committees) if isinstance(committees, list) else str(committees)

            return {
                'source': source,
                'politician': name,
                'party': party,
                'ticker': ticker,
                'direction': direction,
                'amount': amount,
                'disclosure_date': disclosure_raw,
                'transaction_date': tx_raw or disclosure_raw,
                'asset_description': item.get('asset_description') or item.get('company') or ticker,
                'district': item.get('district') or item.get('state') or '',
                'committee_hint': committee_hint,
            }
        except Exception:
            return None

    def fetch_uw_recent_trades(self, days_back: int = 30) -> List[Dict]:
        """Fetch from UW /api/congress/recent-trades"""
        api_key = self._get_api_key()
        if not api_key:
            logger.error("  ❌ UW API key not found — skipping recent trades fetch")
            return []

        cutoff = datetime.now() - timedelta(days=days_back)
        params = {'limit': 200}
        data = _uw_get(UW_RECENT_TRADES_URL, api_key, params=params)
        if not data:
            logger.warning("  ⚠️ UW recent-trades returned no data")
            return []

        items = data.get('data', data) if isinstance(data, dict) else data
        if not isinstance(items, list):
            logger.warning(f"  ⚠️ Unexpected UW recent-trades response shape: {type(items)}")
            return []

        trades = []
        for item in items:
            trade = self._parse_uw_trade(item)
            if not trade:
                continue
            try:
                dt = datetime.strptime(trade['disclosure_date'], '%Y-%m-%d')
                if dt < cutoff:
                    continue
            except ValueError:
                continue
            trades.append(trade)

        logger.info(f"  🏛️ UW recent trades: {len(trades)} in last {days_back} days")
        return trades

    def fetch_uw_portfolio_trades(self, days_back: int = 30) -> List[Dict]:
        """Fetch from UW /api/politician-portfolios/recent_trades"""
        api_key = self._get_api_key()
        if not api_key:
            return []

        cutoff = datetime.now() - timedelta(days=days_back)
        params = {'limit': 200}
        data = _uw_get(UW_PORTFOLIO_TRADES_URL, api_key, params=params)
        if not data:
            logger.warning("  ⚠️ UW portfolio-trades returned no data")
            return []

        items = data.get('data', data) if isinstance(data, dict) else data
        if not isinstance(items, list):
            logger.warning(f"  ⚠️ Unexpected UW portfolio-trades response shape: {type(items)}")
            return []

        trades = []
        for item in items:
            trade = self._parse_uw_trade(item)
            if not trade:
                continue
            try:
                dt = datetime.strptime(trade['disclosure_date'], '%Y-%m-%d')
                if dt < cutoff:
                    continue
            except ValueError:
                continue
            trades.append(trade)

        logger.info(f"  🏛️ UW portfolio trades: {len(trades)} in last {days_back} days")
        return trades

    def fetch_sec_form4(self, tickers: List[str] = None, days_back: int = 7) -> List[Dict]:
        """
        Fetch SEC Form 4 insider transaction filings from EDGAR.
        Form 4 = Statement of Changes in Beneficial Ownership
        Covers: directors, officers, 10%+ shareholders
        """
        try:
            start_date = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')
            params = {
                'q': '"form 4"',
                'dateRange': 'custom',
                'startdt': start_date,
                'forms': '4',
                '_source': 'hits.hits._source',
                'hits.hits.total.value': 1,
            }

            # Use EDGAR full-text search
            url = "https://efts.sec.gov/LATEST/search-index?q=%22ownership%22&forms=4"
            if tickers:
                ticker_query = '+'.join(tickers[:5])  # Limit to 5 tickers per query
                url = f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker_query}%22&forms=4&dateRange=custom&startdt={start_date}"

            data = _safe_get(url, timeout=10)
            if not data:
                # Try simpler EDGAR company search endpoint
                return self._fetch_edgar_recent_form4()

            hits = []
            if isinstance(data, dict):
                hits = data.get('hits', {}).get('hits', [])

            filings = []
            for hit in hits[:20]:  # Cap at 20
                src = hit.get('_source', {})
                ticker = src.get('period_of_report', '')
                filings.append({
                    'source': 'sec_form4',
                    'entity_name': src.get('entity_name', ''),
                    'form_type': '4',
                    'filed_at': src.get('file_date', ''),
                    'ticker': src.get('ticker', ''),
                    'description': src.get('file_description', ''),
                    'url': src.get('file_date', '')
                })

            logger.info(f"  📋 SEC Form 4: {len(filings)} recent insider filings")
            return filings

        except Exception as e:
            logger.debug(f"SEC Form 4 fetch failed: {e}")
            return []

    def _fetch_edgar_recent_form4(self) -> List[Dict]:
        """Simpler EDGAR Form 4 fetch via full-text search API"""
        try:
            url = "https://efts.sec.gov/LATEST/search-index?q=%22purchased%22+%22shares%22&forms=4"
            data = _safe_get(url, timeout=10)
            if not data:
                return []

            hits = data.get('hits', {}).get('hits', []) if isinstance(data, dict) else []
            filings = []
            for h in hits[:10]:
                src = h.get('_source', {})
                filings.append({
                    'source': 'sec_form4',
                    'entity_name': src.get('entity_name', ''),
                    'form_type': '4',
                    'filed_at': src.get('file_date', ''),
                    'ticker': '',
                    'description': 'Insider purchase/sale',
                    'url': ''
                })
            return filings
        except Exception:
            return []

    def detect_cluster_buys(self, trades: List[Dict], window_days: int = CLUSTER_WINDOW_DAYS,
                             min_count: int = CLUSTER_MIN_COUNT) -> List[Dict]:
        """
        Detect when 3+ politicians buy the same stock within a rolling window.
        This is the highest-alpha signal: it suggests committee-level intelligence.

        Returns list of cluster signals sorted by count descending.
        """
        cutoff = datetime.now() - timedelta(days=window_days)
        buy_trades = [t for t in trades if t['direction'] == 'buy']

        # Group by ticker
        ticker_groups: Dict[str, List[Dict]] = {}
        for trade in buy_trades:
            ticker = trade['ticker']
            try:
                dt = datetime.strptime(trade['disclosure_date'], '%Y-%m-%d')
            except ValueError:
                continue
            if dt < cutoff:
                continue
            if ticker not in ticker_groups:
                ticker_groups[ticker] = []
            ticker_groups[ticker].append(trade)

        clusters = []
        for ticker, group in ticker_groups.items():
            if len(group) >= min_count:
                politicians = list({t['politician'] for t in group})
                total_amount = sum(t['amount'] for t in group if t['amount'])
                parties = list({t['party'] for t in group})
                bipartisan = len(parties) > 1

                # Detect committee relevance
                committee_relevance = self._get_committee_relevance(ticker)

                clusters.append({
                    'ticker': ticker,
                    'buy_count': len(group),
                    'politicians': politicians,
                    'total_estimated_amount': total_amount,
                    'bipartisan': bipartisan,
                    'parties': parties,
                    'committee_relevance': committee_relevance,
                    'window_days': window_days,
                    'signal_strength': self._score_cluster(len(group), total_amount, bipartisan, committee_relevance)
                })

        clusters.sort(key=lambda x: x['signal_strength'], reverse=True)
        return clusters

    def _get_committee_relevance(self, ticker: str) -> List[str]:
        """Return committees that have intel relevance to this ticker"""
        relevant = []
        for committee, tickers in COMMITTEE_INTEL_MAP.items():
            if ticker in tickers:
                relevant.append(committee)
        return relevant

    def _score_cluster(self, count: int, total_amount: float, bipartisan: bool,
                        committee_relevance: List[str]) -> float:
        """Score a cluster signal 0-100"""
        score = 0.0
        # More politicians = stronger signal (base: 10 pts per politician, max 50)
        score += min(50, count * 10)
        # Total dollar size matters (log scale)
        if total_amount > 0:
            import math
            score += min(20, math.log10(max(1, total_amount)) * 3)
        # Bipartisan = very strong (both sides rarely agree)
        if bipartisan:
            score += 15
        # Committee relevance = very high alpha
        if committee_relevance:
            score += 15
        return min(100, score)

    def _parse_amount_range(self, amount_str: str) -> float:
        """Parse amount ranges like '$15,001 - $50,000' to midpoint"""
        if not amount_str:
            return 0.0
        try:
            # Remove $ and commas
            clean = amount_str.replace('$', '').replace(',', '').strip()
            if ' - ' in clean:
                parts = clean.split(' - ')
                low = float(parts[0].strip())
                high = float(parts[1].strip())
                return (low + high) / 2
            elif '-' in clean and not clean.startswith('-'):
                parts = clean.split('-')
                if len(parts) == 2:
                    low = float(parts[0].strip())
                    high = float(parts[1].strip())
                    return (low + high) / 2
            else:
                return float(clean)
        except (ValueError, IndexError):
            return 0.0

    def save_trades_to_db(self, trades: List[Dict]) -> int:
        """Save congressional trades to database, returning count of new records"""
        if not trades:
            return 0

        conn = get_sqlite_conn(str(self.db_path), timeout=15)
        conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.cursor()

        saved = 0
        for trade in trades:
            try:
                cursor.execute('''
                    INSERT OR IGNORE INTO congressional_trades
                    (source, politician, party, ticker, direction, amount,
                     disclosure_date, transaction_date, asset_description,
                     district, committee_hint)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    trade.get('source', ''),
                    trade.get('politician', ''),
                    trade.get('party', ''),
                    trade.get('ticker', ''),
                    trade.get('direction', ''),
                    trade.get('amount', 0),
                    trade.get('disclosure_date', ''),
                    trade.get('transaction_date', ''),
                    trade.get('asset_description', ''),
                    trade.get('district', ''),
                    trade.get('committee_hint', '')
                ))
                if cursor.rowcount > 0:
                    saved += 1
            except Exception as e:
                logger.debug(f"DB insert failed: {e}")
                continue

        conn.commit()
        conn.close()
        return saved

    def save_clusters_to_db(self, clusters: List[Dict]):
        """Save cluster signals to database"""
        if not clusters:
            return

        conn = get_sqlite_conn(str(self.db_path), timeout=15)
        conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.cursor()

        for cluster in clusters:
            try:
                cursor.execute('''
                    INSERT INTO congressional_cluster_signals
                    (ticker, buy_count, politicians_json, total_amount,
                     bipartisan, committee_relevance, signal_strength, window_days)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    cluster['ticker'],
                    cluster['buy_count'],
                    json.dumps(cluster['politicians']),
                    cluster['total_estimated_amount'],
                    1 if cluster['bipartisan'] else 0,
                    json.dumps(cluster['committee_relevance']),
                    cluster['signal_strength'],
                    cluster['window_days']
                ))
            except Exception as e:
                logger.debug(f"Cluster DB insert failed: {e}")

        conn.commit()
        conn.close()

    def run_full_scan(self, days_back: int = 30) -> Dict:
        """
        Main entry point: fetch all congressional data, detect signals, save to DB.

        Returns summary dict for use in Slack notification and Gemini context.
        """
        logger.info("🏛️ Congressional Trading Tracker: running full scan...")

        all_trades = []

        # Fetch from Unusual Whales /api/congress/recent-trades
        try:
            recent_trades = self.fetch_uw_recent_trades(days_back=days_back)
            all_trades.extend(recent_trades)
        except Exception as e:
            logger.warning(f"  ⚠️ UW recent-trades fetch error: {e}")

        # Fetch from Unusual Whales /api/politician-portfolios/recent_trades
        try:
            portfolio_trades = self.fetch_uw_portfolio_trades(days_back=days_back)
            # Deduplicate by (politician, ticker, transaction_date) against what we already have
            seen = {(t['politician'], t['ticker'], t['transaction_date']) for t in all_trades}
            for t in portfolio_trades:
                key = (t['politician'], t['ticker'], t['transaction_date'])
                if key not in seen:
                    all_trades.append(t)
                    seen.add(key)
        except Exception as e:
            logger.warning(f"  ⚠️ UW portfolio-trades fetch error: {e}")

        logger.info(f"  🏛️ Total congressional trades found: {len(all_trades)}")

        # Filter to meaningful size
        significant_trades = [t for t in all_trades if t.get('amount', 0) >= MIN_TRADE_SIZE]
        logger.info(f"  🏛️ Significant trades (>${MIN_TRADE_SIZE:,}): {len(significant_trades)}")

        # Save trades
        new_saved = self.save_trades_to_db(significant_trades)
        logger.info(f"  💾 Saved {new_saved} new trades to DB")

        # Detect cluster buys
        clusters = self.detect_cluster_buys(all_trades)
        if clusters:
            logger.info(f"  🎯 Cluster signals detected: {len(clusters)}")
            for cluster in clusters[:3]:
                logger.info(f"    {cluster['ticker']}: {cluster['buy_count']} politicians, "
                            f"strength={cluster['signal_strength']:.1f}, "
                            f"bipartisan={cluster['bipartisan']}")
            self.save_clusters_to_db(clusters)

        # Summary for orchestrator
        top_buys = sorted(
            [t for t in significant_trades if t['direction'] == 'buy'],
            key=lambda x: x.get('amount', 0),
            reverse=True
        )[:5]

        top_sells = sorted(
            [t for t in significant_trades if t['direction'] == 'sell'],
            key=lambda x: x.get('amount', 0),
            reverse=True
        )[:5]

        return {
            'total_trades': len(all_trades),
            'significant_trades': len(significant_trades),
            'new_records_saved': new_saved,
            'clusters': clusters[:5],  # Top 5 cluster signals
            'top_buys': top_buys,
            'top_sells': top_sells,
            'has_clusters': len(clusters) > 0,
            'top_cluster_ticker': clusters[0]['ticker'] if clusters else None,
            'top_cluster_strength': clusters[0]['signal_strength'] if clusters else 0,
            'top_cluster_bipartisan': clusters[0]['bipartisan'] if clusters else False,
            'scan_timestamp': datetime.now().isoformat()
        }


# Standalone test
if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    tracker = CongressionalTracker()
    print("\n🏛️ Congressional Trading Tracker Test\n" + "="*60)

    # Run scan
    result = tracker.run_full_scan(days_back=30)

    print(f"\n📊 Summary:")
    print(f"  Total trades found: {result['total_trades']}")
    print(f"  Significant (>${MIN_TRADE_SIZE:,}): {result['significant_trades']}")
    print(f"  New DB records: {result['new_records_saved']}")

    if result['clusters']:
        print(f"\n🎯 Cluster Buy Signals:")
        for c in result['clusters'][:5]:
            bipartisan_flag = " [BIPARTISAN]" if c['bipartisan'] else ""
            committee_flag = f" [{', '.join(c['committee_relevance'])}]" if c['committee_relevance'] else ""
            print(f"  {c['ticker']}: {c['buy_count']} politicians | "
                  f"strength={c['signal_strength']:.1f}{bipartisan_flag}{committee_flag}")
            print(f"    Politicians: {', '.join(c['politicians'][:3])}")

    if result['top_buys']:
        print(f"\n💰 Top Buys:")
        for t in result['top_buys'][:3]:
            print(f"  {t['politician']} ({t['party']}) → BUY {t['ticker']} ~${t['amount']:,.0f} [{t['disclosure_date']}]")

    if result['top_sells']:
        print(f"\n📉 Top Sells:")
        for t in result['top_sells'][:3]:
            print(f"  {t['politician']} ({t['party']}) → SELL {t['ticker']} ~${t['amount']:,.0f} [{t['disclosure_date']}]")
