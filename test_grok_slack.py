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
    logger.info("🚀 Triggering manual AI analysis (Gemini 3.1 + Grok-3)...")
    
    # 1. Initialize components
    gemini = GeminiAnalyzer()
    grok = GrokAnalyzer()
    alerts = AlertSystem()
    sector = SectorRotationAnalyzer()
    vix = VIXTermStructure()
    
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
    
    score = 75.0  # Elevated score to trigger Pro/Grok
    crisis_type = 'inflation_rate'
    sentiment_summary = 'Strongly Bearish (90%)'
    components = {'sentiment_net': 90.0, 'urgency_premium': 20.0, 'signal_concentration': 85.0}
    
    # 3. Gather macro context
    sector_res = sector.get_rotation_data()
    vix_res = vix.get_term_structure_data()
    
    # 4. Run AI Analysis
    logger.info("  🤖 Running Gemini Flash...")
    flash_res = gemini.run_flash_analysis(
        test_articles, components, sentiment_summary, crisis_type,
        sector_rotation=sector_res, vix_term_structure=vix_res
    )
    
    logger.info("  🧠 Running Gemini 3.1 Pro...")
    pro_res = gemini.run_pro_analysis(
        test_articles, components, sentiment_summary, crisis_type,
        news_score=score, flash_analysis=flash_res, current_defcon=5,
        sector_rotation=sector_res, vix_term_structure=vix_res
    )
    
    logger.info("  𝕏 Running Grok-3 Second Opinion...")
    grok_res = grok.run_analysis(
        test_articles, crisis_type, score,
        sector_rotation=sector_res, vix_term_structure=vix_res
    )
    
    # 5. Format and Send to Slack
    gemini_summary = {
        'action': pro_res.get('recommended_action', 'WAIT') if pro_res else 'WAIT',
        'theme': pro_res.get('dominant_theme', 'N/A') if pro_res else 'N/A',
        'reasoning': pro_res.get('reasoning', '')[:300] if pro_res else 'N/A'
    }
    
    grok_summary = {
        'action': grok_res.get('second_opinion_action', 'WAIT') if grok_res else 'WAIT',
        'x_sentiment': grok_res.get('x_sentiment_score', 0) if grok_res else 0,
        'reasoning': grok_res.get('reasoning', '')[:300] if grok_res else 'N/A'
    }
    
    logger.info("  📤 Sending to Slack...")
    alerts.send_silent_log('news_update', {
        'news_score': score,
        'crisis_type': crisis_type,
        'sentiment': 'bearish',
        'article_count': len(test_articles),
        'new_article_count': len(test_articles),
        'breaking_count': 1,
        'score_components': components,
        'top_articles': test_articles,
        'gemini': gemini_summary,
        'grok': grok_summary,
        'timestamp': datetime.now().isoformat(),
        'is_test': True
    })
    
    logger.info("✅ Test analysis complete and sent to Slack!")

if __name__ == "__main__":
    trigger_test_analysis()
