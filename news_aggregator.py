#!/usr/bin/env python3
"""
News Aggregator Module
Fetches news from multiple sources: Alpha Vantage API, RSS feeds, and Reddit
"""

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Dict
import hashlib

import requests
import feedparser
from pytz import timezone

# Import deduplicator
try:
    from news_deduplicator import NewsDeduplicator
    DEDUPLICATOR_AVAILABLE = True
except ImportError:
    DEDUPLICATOR_AVAILABLE = False

# Import rate limiter
try:
    from rate_limiter import RateLimiter
    RATE_LIMITER_AVAILABLE = True
except ImportError:
    RATE_LIMITER_AVAILABLE = False

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class NewsArticle:
    """Standardized news article format"""
    title: str
    description: str
    source: str
    published_at: datetime
    url: str
    relevance_score: float = 0.0

    def to_dict(self):
        """Convert to dictionary for JSON serialization"""
        return {
            'title': self.title,
            'description': self.description,
            'source': self.source,
            'published_at': self.published_at.isoformat(),
            'url': self.url,
            'relevance_score': self.relevance_score
        }

    def get_hash(self):
        """Generate unique hash for deduplication"""
        content = f"{self.title}{self.url}".lower()
        return hashlib.md5(content.encode()).hexdigest()


class NewsCache:
    """SQLite-based cache for news articles"""

    def __init__(self, db_path: str, ttl_minutes: int = 15):
        self.db_path = db_path
        self.ttl_minutes = ttl_minutes
        self._init_db()

    def _init_db(self):
        """Initialize cache database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS news_cache (
                article_hash TEXT PRIMARY KEY,
                article_json TEXT,
                cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()

    def get(self, article_hash: str) -> Optional[Dict]:
        """Retrieve cached article if not expired"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cutoff_time = datetime.now() - timedelta(minutes=self.ttl_minutes)
        cursor.execute("""
            SELECT article_json FROM news_cache
            WHERE article_hash = ? AND cached_at > ?
        """, (article_hash, cutoff_time))

        result = cursor.fetchone()
        conn.close()

        if result:
            return json.loads(result[0])
        return None

    def set(self, article_hash: str, article_data: Dict):
        """Cache article data"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            INSERT OR REPLACE INTO news_cache (article_hash, article_json, cached_at)
            VALUES (?, ?, ?)
        """, (article_hash, json.dumps(article_data), datetime.now()))

        conn.commit()
        conn.close()

    def cleanup_expired(self):
        """Remove expired cache entries"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cutoff_time = datetime.now() - timedelta(minutes=self.ttl_minutes)
        cursor.execute("DELETE FROM news_cache WHERE cached_at < ?", (cutoff_time,))

        conn.commit()
        conn.close()


class AlphaVantageNewsSource:
    """Fetch news from Alpha Vantage News API"""

    BASE_URL = "https://www.alphavantage.co/query"

    def __init__(self, api_key: str, topics: List[str], max_articles: int = 50, timeout: int = 10, rate_limiter: Optional['RateLimiter'] = None):
        self.api_key = api_key
        self.topics = topics
        self.max_articles = max_articles
        self.timeout = timeout
        self.rate_limiter = rate_limiter

        # Configure rate limiter (Alpha Vantage free tier: 5 calls/min)
        if self.rate_limiter:
            self.rate_limiter.configure('alpha_vantage', requests_per_minute=5, min_delay_seconds=12)

    def fetch_news(self) -> List[NewsArticle]:
        """Fetch news from Alpha Vantage"""
        try:
            # Wait for rate limiter if configured
            if self.rate_limiter:
                self.rate_limiter.wait_if_needed('alpha_vantage')

            params = {
                'function': 'NEWS_SENTIMENT',
                'topics': ','.join(self.topics),
                'apikey': self.api_key,
                'limit': self.max_articles
            }

            logger.info(f"Fetching news from Alpha Vantage (topics: {self.topics})")
            response = requests.get(self.BASE_URL, params=params, timeout=self.timeout)
            response.raise_for_status()

            data = response.json()

            # Check for API errors
            if 'Error Message' in data:
                logger.error(f"Alpha Vantage API error: {data['Error Message']}")
                if self.rate_limiter:
                    self.rate_limiter.record_request('alpha_vantage', success=False)
                return []

            if 'Note' in data:
                logger.warning(f"Alpha Vantage rate limit: {data['Note']}")
                if self.rate_limiter:
                    self.rate_limiter.trigger_backoff('alpha_vantage', error_code=429)
                return []

            articles = []
            for item in data.get('feed', []):
                try:
                    article = NewsArticle(
                        title=item.get('title', ''),
                        description=item.get('summary', ''),
                        source='AlphaVantage',
                        published_at=datetime.strptime(item['time_published'], '%Y%m%dT%H%M%S'),
                        url=item.get('url', ''),
                        relevance_score=float(item.get('overall_sentiment_score', 0)) * 100
                    )
                    articles.append(article)
                except (KeyError, ValueError) as e:
                    logger.debug(f"Skipping malformed Alpha Vantage article: {e}")
                    continue

            logger.info(f"Fetched {len(articles)} articles from Alpha Vantage")

            # Record successful request
            if self.rate_limiter:
                self.rate_limiter.record_request('alpha_vantage', success=True)

            return articles

        except requests.exceptions.Timeout:
            logger.warning(f"Alpha Vantage request timed out after {self.timeout}s")
            if self.rate_limiter:
                self.rate_limiter.record_request('alpha_vantage', success=False)
            return []
        except requests.exceptions.RequestException as e:
            logger.error(f"Alpha Vantage request failed: {e}")
            if self.rate_limiter:
                # Check if it's a rate limit error (429)
                if hasattr(e, 'response') and e.response is not None and e.response.status_code == 429:
                    self.rate_limiter.trigger_backoff('alpha_vantage', error_code=429)
                else:
                    self.rate_limiter.record_request('alpha_vantage', success=False)
            return []
        except Exception as e:
            logger.error(f"Unexpected error fetching Alpha Vantage news: {e}")
            if self.rate_limiter:
                self.rate_limiter.record_request('alpha_vantage', success=False)
            return []


class RSSFeedSource:
    """Fetch news from RSS feeds"""

    def __init__(self, feeds: List[str], timeout: int = 15):
        self.feeds = feeds
        self.timeout = timeout

    def fetch_news(self) -> List[NewsArticle]:
        """Fetch news from all RSS feeds"""
        all_articles = []

        for feed_url in self.feeds:
            try:
                logger.info(f"Fetching RSS feed: {feed_url}")
                feed = feedparser.parse(feed_url)

                for entry in feed.entries:
                    try:
                        # Parse publish date
                        if hasattr(entry, 'published_parsed') and entry.published_parsed:
                            pub_date = datetime(*entry.published_parsed[:6])
                        elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                            pub_date = datetime(*entry.updated_parsed[:6])
                        else:
                            pub_date = datetime.now()

                        article = NewsArticle(
                            title=entry.get('title', ''),
                            description=entry.get('summary', entry.get('description', '')),
                            source=f"RSS-{feed.feed.get('title', 'Unknown')}",
                            published_at=pub_date,
                            url=entry.get('link', ''),
                            relevance_score=50.0  # Default relevance
                        )
                        all_articles.append(article)

                    except Exception as e:
                        logger.debug(f"Skipping malformed RSS entry: {e}")
                        continue

                logger.info(f"Fetched {len(feed.entries)} articles from {feed_url}")

            except Exception as e:
                logger.warning(f"Failed to fetch RSS feed {feed_url}: {e}")
                continue

        return all_articles


class RedditSentimentSource:
    """Scrape Reddit for market sentiment"""

    def __init__(self, subreddits: List[str], post_limit: int = 50, timeout: int = 10, rate_limiter: Optional['RateLimiter'] = None):
        self.subreddits = subreddits
        self.post_limit = post_limit
        self.timeout = timeout
        self.rate_limiter = rate_limiter

        # Configure rate limiter (Reddit: 60 requests/min for unauthenticated)
        if self.rate_limiter:
            self.rate_limiter.configure('reddit', requests_per_minute=60, min_delay_seconds=1)

    def fetch_sentiment(self) -> List[NewsArticle]:
        """Fetch hot posts from specified subreddits"""
        all_articles = []

        headers = {
            'User-Agent': 'HighTrade/1.0'
        }

        for subreddit in self.subreddits:
            try:
                # Wait for rate limiter if configured
                if self.rate_limiter:
                    self.rate_limiter.wait_if_needed('reddit')

                url = f"https://www.reddit.com/r/{subreddit}/hot.json?limit={self.post_limit}"
                logger.info(f"Fetching Reddit: r/{subreddit}")

                response = requests.get(url, headers=headers, timeout=self.timeout)
                response.raise_for_status()

                data = response.json()

                # Record successful request
                if self.rate_limiter:
                    self.rate_limiter.record_request('reddit', success=True)

                for post in data.get('data', {}).get('children', []):
                    try:
                        post_data = post.get('data', {})

                        # Calculate relevance from upvote ratio and score
                        upvote_ratio = post_data.get('upvote_ratio', 0.5)
                        score = post_data.get('score', 0)
                        relevance = min(100, (upvote_ratio * score) / 10)

                        # Convert timestamp
                        created_utc = post_data.get('created_utc', time.time())
                        pub_date = datetime.fromtimestamp(created_utc)

                        article = NewsArticle(
                            title=post_data.get('title', ''),
                            description=post_data.get('selftext', '')[:500],  # Truncate long posts
                            source=f"Reddit-r/{subreddit}",
                            published_at=pub_date,
                            url=f"https://reddit.com{post_data.get('permalink', '')}",
                            relevance_score=relevance
                        )
                        all_articles.append(article)

                    except Exception as e:
                        logger.debug(f"Skipping malformed Reddit post: {e}")
                        continue

                logger.info(f"Fetched {len(data.get('data', {}).get('children', []))} posts from r/{subreddit}")

            except requests.exceptions.RequestException as e:
                logger.warning(f"Failed to fetch r/{subreddit}: {e}")
                if self.rate_limiter:
                    # Check for rate limit (429)
                    if hasattr(e, 'response') and e.response is not None and e.response.status_code == 429:
                        self.rate_limiter.trigger_backoff('reddit', error_code=429)
                    else:
                        self.rate_limiter.record_request('reddit', success=False)
                continue
            except Exception as e:
                logger.error(f"Unexpected error fetching r/{subreddit}: {e}")
                if self.rate_limiter:
                    self.rate_limiter.record_request('reddit', success=False)
                continue

        return all_articles


class NewsAggregator:
    """Main news aggregator that orchestrates all sources"""

    def __init__(self, config_path: str = 'news_config.json'):
        self.config = self._load_config(config_path)
        self.sources = {}
        self.cache = None
        self.deduplicator = None
        self.rate_limiter = None

        self._init_rate_limiter()
        self._init_sources()
        self._init_cache()
        self._init_deduplicator()

    def _load_config(self, config_path: str) -> Dict:
        """Load configuration from JSON file"""
        config_file = Path(config_path)
        if not config_file.exists():
            logger.warning(f"Config file not found: {config_path}, using defaults")
            return self._get_default_config()

        with open(config_file, 'r') as f:
            return json.load(f)

    def _get_default_config(self) -> Dict:
        """Return default configuration"""
        return {
            'sources': {
                'alpha_vantage': {'enabled': False},
                'rss_feeds': {'enabled': False},
                'reddit': {'enabled': False}
            },
            'caching': {'enabled': False}
        }

    def _init_sources(self):
        """Initialize enabled news sources"""
        # Alpha Vantage
        if self.config['sources']['alpha_vantage'].get('enabled', False):
            av_config = self.config['sources']['alpha_vantage']
            self.sources['alpha_vantage'] = AlphaVantageNewsSource(
                api_key=av_config['api_key'],
                topics=av_config.get('topics', ['market']),
                max_articles=av_config.get('max_articles', 50),
                timeout=av_config.get('timeout_seconds', 10),
                rate_limiter=self.rate_limiter
            )
            logger.info("Alpha Vantage news source enabled")

        # RSS Feeds
        if self.config['sources']['rss_feeds'].get('enabled', False):
            rss_config = self.config['sources']['rss_feeds']
            self.sources['rss_feeds'] = RSSFeedSource(
                feeds=rss_config.get('feeds', []),
                timeout=rss_config.get('timeout_seconds', 15)
            )
            logger.info("RSS feed source enabled")

        # Reddit
        if self.config['sources']['reddit'].get('enabled', False):
            reddit_config = self.config['sources']['reddit']
            self.sources['reddit'] = RedditSentimentSource(
                subreddits=reddit_config.get('subreddits', []),
                post_limit=reddit_config.get('post_limit', 50),
                timeout=reddit_config.get('timeout_seconds', 10),
                rate_limiter=self.rate_limiter
            )
            logger.info("Reddit sentiment source enabled")

    def _init_cache(self):
        """Initialize caching if enabled"""
        if self.config.get('caching', {}).get('enabled', False):
            cache_path = self.config['caching'].get('cache_path', 'trading_data/news_cache.db')
            ttl_minutes = self.config['caching'].get('ttl_minutes', 15)
            self.cache = NewsCache(cache_path, ttl_minutes)
            logger.info(f"News cache enabled (TTL: {ttl_minutes} minutes)")

    def _init_rate_limiter(self):
        """Initialize rate limiter for API calls"""
        if RATE_LIMITER_AVAILABLE:
            self.rate_limiter = RateLimiter()
            logger.info("Rate limiting enabled for API sources")
        else:
            logger.warning("RateLimiter not available - no protection against rate limits")

    def _init_deduplicator(self):
        """Initialize content-based deduplicator"""
        if DEDUPLICATOR_AVAILABLE:
            # Get similarity threshold from config (default 0.6 for good balance)
            threshold = self.config.get('deduplication', {}).get('similarity_threshold', 0.6)
            self.deduplicator = NewsDeduplicator(similarity_threshold=threshold)
            logger.info(f"Content deduplication enabled (threshold: {threshold})")
        else:
            logger.warning("NewsDeduplicator not available - using basic hash dedup only")

    def fetch_latest_news(self, lookback_hours: int = 1) -> List[NewsArticle]:
        """
        Fetch news from all enabled sources

        Args:
            lookback_hours: Only return articles published within this timeframe

        Returns:
            List of NewsArticle objects, deduplicated and sorted by relevance
        """
        all_articles = []
        cutoff_time = datetime.now() - timedelta(hours=lookback_hours)

        # Fetch from Alpha Vantage
        if 'alpha_vantage' in self.sources:
            try:
                av_articles = self.sources['alpha_vantage'].fetch_news()
                all_articles.extend(av_articles)
            except Exception as e:
                logger.error(f"Alpha Vantage fetch failed: {e}")

        # Fetch from RSS feeds
        if 'rss_feeds' in self.sources:
            try:
                rss_articles = self.sources['rss_feeds'].fetch_news()
                all_articles.extend(rss_articles)
            except Exception as e:
                logger.error(f"RSS fetch failed: {e}")

        # Fetch from Reddit
        if 'reddit' in self.sources:
            try:
                reddit_articles = self.sources['reddit'].fetch_sentiment()
                all_articles.extend(reddit_articles)
            except Exception as e:
                logger.error(f"Reddit fetch failed: {e}")

        # Filter by time
        recent_articles = [
            article for article in all_articles
            if article.published_at >= cutoff_time
        ]

        # Step 1: Basic hash-based deduplication (exact URL/title matches)
        seen_hashes = set()
        hash_unique = []
        for article in recent_articles:
            article_hash = article.get_hash()
            if article_hash not in seen_hashes:
                seen_hashes.add(article_hash)
                hash_unique.append(article)

        # Step 2: Content-based similarity deduplication
        if self.deduplicator:
            unique_articles, num_removed = self.deduplicator.deduplicate(
                hash_unique,
                keep_strategy='highest_relevance'
            )
            logger.info(f"Deduplication: {len(recent_articles)} articles → {len(hash_unique)} after hash → {len(unique_articles)} after similarity")
        else:
            unique_articles = hash_unique
            logger.info(f"Aggregated {len(unique_articles)} unique articles from {len(self.sources)} sources")

        # Sort by relevance score (descending)
        unique_articles.sort(key=lambda x: x.relevance_score, reverse=True)

        return unique_articles

    def get_breaking_news(self, window_minutes: int = 30) -> List[NewsArticle]:
        """Get only high-urgency recent news"""
        breaking_articles = self.fetch_latest_news(lookback_hours=window_minutes/60)

        # Filter for high relevance
        breaking_articles = [
            article for article in breaking_articles
            if article.relevance_score >= 70
        ]

        return breaking_articles


# Standalone test
if __name__ == '__main__':
    print("Testing News Aggregator...")

    # Test with config file
    aggregator = NewsAggregator('news_config.json')

    print("\nFetching latest news (last 24 hours)...")
    articles = aggregator.fetch_latest_news(lookback_hours=24)

    print(f"\nFound {len(articles)} articles:")
    for i, article in enumerate(articles[:10], 1):
        print(f"\n{i}. [{article.source}] {article.title}")
        print(f"   Published: {article.published_at}")
        print(f"   Relevance: {article.relevance_score:.1f}/100")
        print(f"   URL: {article.url[:80]}...")

    print("\n\nFetching breaking news (last 30 minutes)...")
    breaking = aggregator.get_breaking_news(window_minutes=30)
    print(f"Found {len(breaking)} breaking news articles")
