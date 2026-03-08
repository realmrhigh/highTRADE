#!/usr/bin/env python3
"""
test_grok_slack.py — Manually trigger a high-score news analysis to verify Gemini 3.1 + Grok-3 + Slack.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Import HighTrade components
from gemini_analyzer import GeminiAnalyzer, GrokAnalyzer
from alerts import AlertSystem
from sector_rotation import SectorRotationAnalyzer
from vix_term_structure import VIXTermStructure

def trigger_test_analysis():
    logger.info("🚀 Triggering manual AI analysis (Gemini Flash + Grok Deep)...")

    # 1. Initialize components
    gemini = GeminiAnalyzer()
    grok   = GrokAnalyzer()
    alerts = AlertSystem()
    sector = SectorRotationAnalyzer()
    vix    = VIXTermStructure()

    # 2. Mock high-impact news
    test_articles = [
        {
            'title': 'BREAKING: Fed signals surprise 50bps rate hike amid inflation surge',
            'description': 'The Federal Reserve has hinted at an aggressive rate hike in its upcoming meeting, citing persistent inflation data. Markets are reacting with a sharp sell-off in tech and growth stocks.',
            'source': 'Bloomberg',
            'published_at': datetime.now().isoformat(),
            'sentiment': 'bearish',
            'urgency': 'high',
            'confidence': 90,
            'matched_keywords': ['Fed', 'rate hike', 'inflation', 'crisis']
        },
        {
            'title': 'Global supply chain disruptions worsen as major ports face labor strikes',
            'description': 'New labor strikes at West Coast ports threaten to freeze imports, adding further pressure to global supply chains and inflation expectations.',
            'source': 'Reuters',
            'published_at': datetime.now().isoformat(),
            'sentiment': 'bearish',
            'urgency': 'high',
            'confidence': 85,
            'matched_keywords': ['supply chain', 'strike', 'inflation']
        }
    ]

    score            = 75.0
    crisis_type      = 'inflation_rate'
    sentiment_summary = 'Strongly Bearish (90%)'
    components       = {'sentiment_net': 90.0, 'urgency_premium': 20.0, 'signal_concentration': 85.0}
    mock_positions   = [
        {'symbol': 'AAPL', 'shares': 10, 'entry_price': 185.50, 'pnl_pct': 2.5},
        {'symbol': 'NVDA', 'shares': 5,  'entry_price': 720.00, 'pnl_pct': -1.2}
    ]

    # 3. Gather macro context
    sector_res = sector.get_rotation_data()
    vix_res    = vix.get_term_structure_data()

    # 4. Run AI Analysis — Flash pre-filter, then Grok deep dive
    logger.info("  🤖 Running Gemini Flash...")
    flash_res = gemini.run_flash_analysis(
        test_articles, components, sentiment_summary, crisis_type,
        sector_rotation=sector_res, vix_term_structure=vix_res
    )

    logger.info("  🧠 Running Grok deep analysis...")
    grok_res = grok.run_deep_analysis(
        test_articles, components, sentiment_summary, crisis_type,
        news_score=score, flash_analysis=flash_res, current_defcon=5,
        positions=mock_positions,
        sector_rotation=sector_res, vix_term_structure=vix_res
    )

    # 5. Format and Send to Slack
    gemini_summary = None
    if flash_res:
        gemini_summary = {
            'action':     flash_res.get('recommended_action', 'WAIT'),
            'coherence':  flash_res.get('narrative_coherence', 0),
            'confidence': flash_res.get('confidence_in_signal', 0),
            'theme':      flash_res.get('dominant_theme', ''),
            'reasoning':  flash_res.get('reasoning', '')[:200],
        }

    logger.info("  📤 Sending to Slack...")
    alerts.send_silent_log('news_update', {
        'news_score':        score,
        'crisis_type':       crisis_type,
        'sentiment':         'bearish',
        'article_count':     len(test_articles),
        'new_article_count': len(test_articles),
        'breaking_count':    1,
        'score_components':  components,
        'top_articles':      test_articles,
        'gemini':            gemini_summary,
        'timestamp':         datetime.now().isoformat(),
        'is_test':           True
    })
    
    logger.info("✅ Test analysis complete and sent to Slack!")

if __name__ == "__main__":
    trigger_test_analysis()
