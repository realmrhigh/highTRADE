#!/usr/bin/env python3
"""
News Signal Generator
Converts news sentiment analysis into trading signals with DEFCON override logic
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class NewsSignalGenerator:
    """Generates trading signals from news sentiment analysis"""

    def __init__(self,
                 breaking_threshold: float = 80.0,
                 high_urgency_threshold: float = 60.0,
                 routine_threshold: float = 30.0):
        """
        Initialize signal generator

        Args:
            breaking_threshold: Score above which news is considered breaking (triggers DEFCON override)
            high_urgency_threshold: Score for high urgency news
            routine_threshold: Score for routine news
        """
        self.signal_thresholds = {
            'breaking_crisis': breaking_threshold,
            'high_urgency': high_urgency_threshold,
            'routine': routine_threshold
        }

    def generate_news_signal(self, articles: List, sentiment_analyzer) -> Dict:
        """
        Generate comprehensive news signal from articles

        Args:
            articles: List of NewsArticle objects
            sentiment_analyzer: NewsSentimentAnalyzer instance

        Returns:
            Dictionary with news signal data:
            {
                'news_score': float (0-100),
                'dominant_crisis_type': str,
                'crisis_description': str,
                'breaking_news_override': bool,
                'recommended_defcon': Optional[int],
                'contributing_articles': List[dict],
                'sentiment_summary': str
            }
        """
        if not articles:
            return self._get_empty_signal()

        # Analyze all articles
        batch_result = sentiment_analyzer.analyze_batch(articles)

        # Calculate news score using weighted formula
        news_score = self._calculate_news_score(articles, batch_result)

        # Determine if DEFCON override is warranted
        breaking_override, recommended_defcon = self._check_defcon_override(
            news_score, batch_result
        )

        # Generate crisis description
        crisis_description = self._generate_crisis_description(
            batch_result, breaking_override
        )

        # Extract contributing articles (top 5 by relevance)
        contributing_articles = self._get_top_articles(articles, batch_result, limit=5)

        # Generate sentiment summary
        sentiment_summary = self._generate_sentiment_summary(batch_result)

        return {
            'news_score': news_score,
            'dominant_crisis_type': batch_result['dominant_crisis_type'],
            'crisis_description': crisis_description,
            'breaking_news_override': breaking_override,
            'recommended_defcon': recommended_defcon if breaking_override else None,
            'contributing_articles': contributing_articles,
            'sentiment_summary': sentiment_summary,
            'article_count': len(articles),
            'breaking_count': batch_result['breaking_count'],
            'avg_confidence': batch_result['avg_confidence']
        }

    def _calculate_news_score(self, articles: List, batch_result: Dict) -> float:
        """
        Calculate composite news score (0-100)

        Formula:
        - Each article contributes based on: confidence * urgency_weight * recency_factor
        - Normalized to 0-100 scale
        """
        news_score = 0.0
        now = datetime.now()

        for article, result in zip(articles, batch_result['results']):
            # Urgency weight
            if result.urgency == 'breaking':
                urgency_weight = 10
            elif result.urgency == 'high':
                urgency_weight = 5
            else:
                urgency_weight = 1

            # Recency decay (articles older than 24 hours decay)
            age_hours = (now - article.published_at).total_seconds() / 3600
            recency_factor = max(0, 1 - (age_hours / 24))

            # Sentiment influence (bearish news increases score more)
            sentiment_multiplier = 1.2 if result.sentiment == 'bearish' else 1.0

            # Calculate contribution
            contribution = (result.confidence * urgency_weight * recency_factor * sentiment_multiplier)
            news_score += contribution

        # Normalize to 0-100 scale
        # Typical range: 0-500, so divide by 10 and cap at 100
        news_score = min(100, news_score / 10)

        logger.info(f"Calculated news score: {news_score:.1f}/100 from {len(articles)} articles")
        return news_score

    def _check_defcon_override(self, news_score: float, batch_result: Dict) -> tuple:
        """
        Determine if news warrants DEFCON override

        Returns:
            (breaking_news_override: bool, recommended_defcon: Optional[int])
        """
        breaking_count = batch_result['breaking_count']
        dominant_sentiment = batch_result['dominant_sentiment']
        avg_confidence = batch_result['avg_confidence']

        # DEFCON 1 conditions: Very high score + multiple breaking articles + bearish
        if (news_score >= 90 and
            breaking_count >= 3 and
            dominant_sentiment == 'bearish'):
            logger.warning(f"🚨 NEWS OVERRIDE TO DEFCON 1: Score={news_score:.1f}, Breaking={breaking_count}")
            return (True, 1)

        # DEFCON 2 conditions: High score + bearish sentiment
        elif (news_score >= self.signal_thresholds['breaking_crisis'] and
              dominant_sentiment == 'bearish'):
            logger.warning(f"⚠️  NEWS OVERRIDE TO DEFCON 2: Score={news_score:.1f}, Sentiment={dominant_sentiment}")
            return (True, 2)

        # No override
        else:
            return (False, None)

    def _generate_crisis_description(self, batch_result: Dict, breaking_override: bool) -> str:
        """Generate human-readable crisis description"""
        crisis_type = batch_result['dominant_crisis_type']
        sentiment = batch_result['dominant_sentiment']
        breaking_count = batch_result['breaking_count']
        total_articles = batch_result['total_articles']

        # Map crisis types to descriptions
        crisis_labels = {
            'tech_crash': 'Technology Sector Crisis',
            'geopolitical_trade': 'Geopolitical/Trade Tensions',
            'liquidity_credit': 'Liquidity/Credit Crisis',
            'inflation_rate': 'Inflation/Fed Policy Crisis',
            'pandemic_health': 'Pandemic/Health Crisis',
            'market_correction': 'Broad Market Correction'
        }

        label = crisis_labels.get(crisis_type, 'Market Event')

        # Build description
        if breaking_override:
            prefix = "🚨 BREAKING"
        else:
            prefix = "📰"

        description = f"{prefix} {label}: {sentiment.upper()} sentiment "
        description += f"({breaking_count} breaking, {total_articles} total articles)"

        return description

    def _get_top_articles(self, articles: List, batch_result: Dict, limit: int = 5) -> List[Dict]:
        """Extract top contributing articles"""
        # Combine articles with their sentiment results
        article_data = []
        for article, result in zip(articles, batch_result['results']):
            article_data.append({
                'title': article.title,
                'source': article.source,
                'published_at': article.published_at.isoformat(),
                'url': article.url,
                'sentiment': result.sentiment,
                'urgency': result.urgency,
                'confidence': result.confidence,
                'crisis_type': result.crisis_type
            })

        # Sort by confidence * urgency
        urgency_scores = {'breaking': 3, 'high': 2, 'routine': 1}
        article_data.sort(
            key=lambda x: x['confidence'] * urgency_scores[x['urgency']],
            reverse=True
        )

        return article_data[:limit]

    def _generate_sentiment_summary(self, batch_result: Dict) -> str:
        """Generate text summary of sentiment"""
        dist = batch_result['sentiment_distribution']
        total = sum(dist.values())

        if total == 0:
            return "No sentiment data"

        bearish_pct = (dist.get('bearish', 0) / total) * 100
        bullish_pct = (dist.get('bullish', 0) / total) * 100
        neutral_pct = (dist.get('neutral', 0) / total) * 100

        return f"Bearish: {bearish_pct:.0f}%, Bullish: {bullish_pct:.0f}%, Neutral: {neutral_pct:.0f}%"

    def _get_empty_signal(self) -> Dict:
        """Return empty signal when no articles available"""
        return {
            'news_score': 0.0,
            'dominant_crisis_type': 'market_correction',
            'crisis_description': 'No news data available',
            'breaking_news_override': False,
            'recommended_defcon': None,
            'contributing_articles': [],
            'sentiment_summary': 'No articles',
            'article_count': 0,
            'breaking_count': 0,
            'avg_confidence': 0.0
        }

    def should_override_defcon(self, news_signal: Dict, current_defcon: int) -> bool:
        """
        Determine if news signal should override current DEFCON level

        Args:
            news_signal: Output from generate_news_signal()
            current_defcon: Current DEFCON level (1-5)

        Returns:
            True if override should occur
        """
        if not news_signal.get('breaking_news_override'):
            return False

        recommended_defcon = news_signal.get('recommended_defcon')
        if recommended_defcon is None:
            return False

        # Only override if news recommends LOWER defcon (higher alert)
        if recommended_defcon < current_defcon:
            logger.warning(
                f"News recommends DEFCON {recommended_defcon} vs current {current_defcon}: "
                f"{news_signal['crisis_description']}"
            )
            return True

        return False


# Standalone test
if __name__ == '__main__':
    from news_aggregator import NewsArticle, NewsAggregator
    from news_sentiment import NewsSentimentAnalyzer
    from datetime import datetime, timedelta

    print("Testing News Signal Generator...\n")

    # Create test articles simulating breaking crisis
    test_articles = [
        NewsArticle(
            title="Fed announces emergency meeting on inflation crisis",
            description="Federal Reserve calls surprise meeting as inflation fears mount, raising concerns about aggressive rate hikes",
            source="AlphaVantage",
            published_at=datetime.now() - timedelta(minutes=5),
            url="http://test.com/1",
            relevance_score=95.0
        ),
        NewsArticle(
            title="Markets plunge as Fed signals emergency action",
            description="Stock markets crash on fears of emergency Fed intervention, credit spreads widen",
            source="RSS-CNBC",
            published_at=datetime.now() - timedelta(minutes=10),
            url="http://test.com/2",
            relevance_score=92.0
        ),
        NewsArticle(
            title="Breaking: Treasury yields spike to 20-year high",
            description="Bond market in crisis as yields surge, raising fears of liquidity crunch",
            source="RSS-MarketWatch",
            published_at=datetime.now() - timedelta(minutes=15),
            url="http://test.com/3",
            relevance_score=90.0
        ),
        NewsArticle(
            title="Analysts warn of potential recession",
            description="Economic indicators point to downturn as Fed tightening accelerates",
            source="Reddit-r/wallstreetbets",
            published_at=datetime.now() - timedelta(hours=1),
            url="http://test.com/4",
            relevance_score=75.0
        )
    ]

    # Initialize components
    sentiment_analyzer = NewsSentimentAnalyzer()
    signal_generator = NewsSignalGenerator()

    # Generate news signal
    print("Generating News Signal...")
    print("=" * 80)
    news_signal = signal_generator.generate_news_signal(test_articles, sentiment_analyzer)

    # Display results
    print(f"\n📊 News Score: {news_signal['news_score']:.1f}/100")
    print(f"🎯 Crisis Type: {news_signal['dominant_crisis_type']}")
    print(f"📝 Description: {news_signal['crisis_description']}")
    print(f"⚡ Breaking Override: {news_signal['breaking_news_override']}")
    if news_signal['recommended_defcon']:
        print(f"🚨 Recommended DEFCON: {news_signal['recommended_defcon']}")
    print(f"📈 Sentiment: {news_signal['sentiment_summary']}")
    print(f"📰 Articles: {news_signal['article_count']} total, {news_signal['breaking_count']} breaking")
    print(f"🎲 Avg Confidence: {news_signal['avg_confidence']:.1f}/100")

    print("\n\nTop Contributing Articles:")
    print("=" * 80)
    for i, article in enumerate(news_signal['contributing_articles'], 1):
        print(f"\n{i}. [{article['source']}] {article['title']}")
        print(f"   Sentiment: {article['sentiment']} | Urgency: {article['urgency']} | Confidence: {article['confidence']:.0f}/100")
        print(f"   URL: {article['url']}")

    # Test DEFCON override logic
    print("\n\nDEFCON Override Testing:")
    print("=" * 80)
    for current_defcon in [5, 4, 3, 2, 1]:
        should_override = signal_generator.should_override_defcon(news_signal, current_defcon)
        print(f"Current DEFCON {current_defcon}: Override = {should_override}")
