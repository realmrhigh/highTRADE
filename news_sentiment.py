#!/usr/bin/env python3
"""
News Sentiment Analysis Module
Analyzes news articles for sentiment, urgency, and crisis pattern matching
"""

import logging
import re
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# Import crisis patterns from paper_trading
CRISIS_PATTERNS = {
    'tech_crash': {
        'keywords': ['tech', 'valuation', 'margin', 'leverage', 'overvalued', 'correction'],
        'rationale': 'Rotate to broad diversification during tech correction'
    },
    'geopolitical_trade': {
        'keywords': ['tariff', 'trade war', 'china', 'supply chain', 'sanctions'],
        'rationale': 'Tech companies resilient to tariffs; focus on IP-based business models'
    },
    'liquidity_credit': {
        'keywords': ['liquidity', 'credit', 'spread', 'financial stress', 'banking', 'crisis'],
        'rationale': 'Large-cap quality less affected by credit stress'
    },
    'inflation_rate': {
        'keywords': ['inflation', 'yield', 'rate', 'fed', 'tightening', 'bonds'],
        'rationale': 'Growth/tech benefit from Fed policy pivot expectations'
    },
    'pandemic_health': {
        'keywords': ['pandemic', 'covid', 'disease', 'health', 'lockdown', 'epidemic'],
        'rationale': 'Work-from-home and cloud infrastructure winners'
    },
    'market_correction': {
        'keywords': ['correction', 'selloff', 'drawdown', 'decline', 'drop', 'crash'],
        'rationale': 'Flight to mega-cap quality and defensive positioning'
    }
}


# Sentiment keywords
BEARISH_KEYWORDS = [
    'crash', 'collapse', 'crisis', 'plunge', 'plummet', 'fear', 'panic',
    'sell-off', 'selloff', 'tumble', 'slump', 'recession', 'depression',
    'downturn', 'bearish', 'negative', 'warning', 'alert', 'emergency',
    'concern', 'worry', 'risk', 'threat', 'decline', 'fall', 'drop'
]

BULLISH_KEYWORDS = [
    'rally', 'surge', 'soar', 'recovery', 'rebound', 'deal', 'agreement',
    'resolution', 'bullish', 'positive', 'optimism', 'growth', 'gain',
    'rise', 'climb', 'advance', 'breakthrough', 'success', 'profit',
    'strong', 'robust', 'improving', 'upturn'
]


@dataclass
class SentimentResult:
    """Result of sentiment analysis on a single article"""
    crisis_type: str
    sentiment: str  # 'bullish', 'bearish', 'neutral'
    urgency: str  # 'breaking', 'high', 'routine'
    confidence: float  # 0-100
    matched_keywords: List[str]
    sentiment_score: float  # -100 to 100


class NewsSentimentAnalyzer:
    """Analyzes news sentiment and matches to crisis patterns"""

    def __init__(self, breaking_window_minutes: int = 30):
        self.breaking_window_minutes = breaking_window_minutes
        self.crisis_patterns = CRISIS_PATTERNS

    def analyze_article(self, article) -> SentimentResult:
        """
        Analyze a single news article

        Args:
            article: NewsArticle object

        Returns:
            SentimentResult with crisis type, sentiment, urgency, etc.
        """
        # Combine title and description for analysis (weight title 3x)
        title_lower = article.title.lower()
        desc_lower = article.description.lower()
        combined_text = (title_lower + ' ' + title_lower + ' ' + title_lower + ' ' + desc_lower)

        # Match to crisis patterns
        crisis_type, crisis_confidence, matched_keywords = self._match_crisis_pattern(combined_text)

        # Analyze sentiment
        sentiment, sentiment_score = self._analyze_sentiment(combined_text)

        # Determine urgency
        urgency = self._classify_urgency(article, crisis_confidence)

        return SentimentResult(
            crisis_type=crisis_type,
            sentiment=sentiment,
            urgency=urgency,
            confidence=crisis_confidence,
            matched_keywords=matched_keywords,
            sentiment_score=sentiment_score
        )

    def analyze_batch(self, articles: List) -> Dict:
        """
        Analyze a batch of articles and return aggregate results

        Args:
            articles: List of NewsArticle objects

        Returns:
            Dictionary with aggregate sentiment metrics
        """
        if not articles:
            return {
                'total_articles': 0,
                'dominant_sentiment': 'neutral',
                'dominant_crisis_type': 'market_correction',
                'breaking_count': 0,
                'avg_confidence': 0,
                'sentiment_distribution': {}
            }

        results = [self.analyze_article(article) for article in articles]

        # Count sentiment types
        sentiment_counts = {'bullish': 0, 'bearish': 0, 'neutral': 0}
        for result in results:
            sentiment_counts[result.sentiment] += 1

        # Count crisis types
        crisis_counts = {}
        for result in results:
            crisis_counts[result.crisis_type] = crisis_counts.get(result.crisis_type, 0) + 1

        # Find dominant sentiment and crisis type
        dominant_sentiment = max(sentiment_counts, key=sentiment_counts.get)
        dominant_crisis = max(crisis_counts, key=crisis_counts.get)

        # Count breaking news
        breaking_count = sum(1 for result in results if result.urgency == 'breaking')

        # Average confidence
        avg_confidence = sum(result.confidence for result in results) / len(results)

        return {
            'total_articles': len(articles),
            'dominant_sentiment': dominant_sentiment,
            'dominant_crisis_type': dominant_crisis,
            'breaking_count': breaking_count,
            'avg_confidence': avg_confidence,
            'sentiment_distribution': sentiment_counts,
            'crisis_distribution': crisis_counts,
            'results': results
        }

    def _match_crisis_pattern(self, text: str) -> Tuple[str, float, List[str]]:
        """
        Match text to crisis patterns

        Returns:
            (crisis_type, confidence_score, matched_keywords)
        """
        pattern_scores = {}
        matched_keywords_per_pattern = {}

        for pattern_type, pattern_data in self.crisis_patterns.items():
            keywords = pattern_data['keywords']
            matched_keywords = []

            for keyword in keywords:
                if keyword in text:
                    matched_keywords.append(keyword)

            # Score based on keyword matches
            if matched_keywords:
                # More matches = higher score
                score = len(matched_keywords) * 15
                # Bonus for multiple unique keyword matches
                score += len(set(matched_keywords)) * 10
                pattern_scores[pattern_type] = min(100, score)
                matched_keywords_per_pattern[pattern_type] = matched_keywords

        # Return best match or default
        if pattern_scores:
            best_pattern = max(pattern_scores, key=pattern_scores.get)
            return (
                best_pattern,
                pattern_scores[best_pattern],
                matched_keywords_per_pattern[best_pattern]
            )
        else:
            return ('market_correction', 30.0, [])

    def _analyze_sentiment(self, text: str) -> Tuple[str, float]:
        """
        Analyze sentiment of text

        Returns:
            (sentiment_label, sentiment_score)
            sentiment_score ranges from -100 (very bearish) to +100 (very bullish)
        """
        # Count keyword matches
        bearish_count = sum(1 for keyword in BEARISH_KEYWORDS if keyword in text)
        bullish_count = sum(1 for keyword in BULLISH_KEYWORDS if keyword in text)

        # Calculate total words (rough estimate)
        word_count = len(text.split())
        if word_count == 0:
            return ('neutral', 0.0)

        # Calculate sentiment score
        sentiment_score = ((bullish_count - bearish_count) / max(1, word_count * 0.01)) * 100
        sentiment_score = max(-100, min(100, sentiment_score))  # Clamp to -100 to 100

        # Classify sentiment
        if sentiment_score < -20:
            sentiment_label = 'bearish'
        elif sentiment_score > 20:
            sentiment_label = 'bullish'
        else:
            sentiment_label = 'neutral'

        return (sentiment_label, sentiment_score)

    def _classify_urgency(self, article, crisis_confidence: float) -> str:
        """
        Classify urgency based on publish time and confidence

        Returns:
            'breaking', 'high', or 'routine'
        """
        # Calculate age of article
        age_minutes = (datetime.now() - article.published_at).total_seconds() / 60

        # Breaking: Very recent + high confidence
        if age_minutes <= self.breaking_window_minutes and crisis_confidence >= 70:
            return 'breaking'

        # High: Recent + moderate confidence
        elif age_minutes <= 120 and crisis_confidence >= 50:
            return 'high'

        # Otherwise routine
        else:
            return 'routine'

    def detect_breaking_crisis(self, articles: List) -> Optional[Dict]:
        """
        Detect if there's a breaking crisis from article clustering

        Returns:
            Crisis alert dict if detected, None otherwise
        """
        # Analyze all articles
        batch_result = self.analyze_batch(articles)

        # Check for breaking crisis conditions:
        # 1. Multiple breaking news articles
        # 2. High average confidence
        # 3. Consistent crisis type

        if batch_result['breaking_count'] >= 3 and batch_result['avg_confidence'] >= 65:
            return {
                'detected': True,
                'crisis_type': batch_result['dominant_crisis_type'],
                'breaking_articles': batch_result['breaking_count'],
                'avg_confidence': batch_result['avg_confidence'],
                'dominant_sentiment': batch_result['dominant_sentiment']
            }

        return None


# Standalone test
if __name__ == '__main__':
    from news_aggregator import NewsArticle
    from datetime import datetime

    print("Testing News Sentiment Analyzer...\n")

    # Test with sample articles
    test_articles = [
        NewsArticle(
            title="Fed announces emergency rate hike amid inflation fears",
            description="The Federal Reserve shocked markets with an unscheduled meeting, raising rates by 75 basis points as inflation concerns mount",
            source="Test",
            published_at=datetime.now(),
            url="http://test.com/1",
            relevance_score=90.0
        ),
        NewsArticle(
            title="Tech stocks plunge on valuation concerns",
            description="Technology sector sees massive selloff as investors worry about overvaluation and margin compression",
            source="Test",
            published_at=datetime.now() - timedelta(minutes=10),
            url="http://test.com/2",
            relevance_score=85.0
        ),
        NewsArticle(
            title="China announces new tariffs in escalating trade war",
            description="Beijing retaliates with tariffs on US goods, raising concerns about supply chain disruption",
            source="Test",
            published_at=datetime.now() - timedelta(minutes=20),
            url="http://test.com/3",
            relevance_score=88.0
        )
    ]

    analyzer = NewsSentimentAnalyzer()

    # Test individual article analysis
    print("Individual Article Analysis:")
    print("=" * 60)
    for i, article in enumerate(test_articles, 1):
        result = analyzer.analyze_article(article)
        print(f"\nArticle {i}: {article.title}")
        print(f"  Crisis Type: {result.crisis_type}")
        print(f"  Sentiment: {result.sentiment} (score: {result.sentiment_score:.1f})")
        print(f"  Urgency: {result.urgency}")
        print(f"  Confidence: {result.confidence:.1f}/100")
        print(f"  Matched Keywords: {', '.join(result.matched_keywords)}")

    # Test batch analysis
    print("\n\nBatch Analysis:")
    print("=" * 60)
    batch_result = analyzer.analyze_batch(test_articles)
    print(f"Total Articles: {batch_result['total_articles']}")
    print(f"Dominant Sentiment: {batch_result['dominant_sentiment']}")
    print(f"Dominant Crisis: {batch_result['dominant_crisis_type']}")
    print(f"Breaking News Count: {batch_result['breaking_count']}")
    print(f"Average Confidence: {batch_result['avg_confidence']:.1f}/100")
    print(f"Sentiment Distribution: {batch_result['sentiment_distribution']}")
    print(f"Crisis Distribution: {batch_result['crisis_distribution']}")

    # Test breaking crisis detection
    print("\n\nBreaking Crisis Detection:")
    print("=" * 60)
    crisis_alert = analyzer.detect_breaking_crisis(test_articles)
    if crisis_alert:
        print("🚨 BREAKING CRISIS DETECTED!")
        print(f"  Crisis Type: {crisis_alert['crisis_type']}")
        print(f"  Breaking Articles: {crisis_alert['breaking_articles']}")
        print(f"  Confidence: {crisis_alert['avg_confidence']:.1f}/100")
        print(f"  Dominant Sentiment: {crisis_alert['dominant_sentiment']}")
    else:
        print("No breaking crisis detected")
