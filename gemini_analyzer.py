#!/usr/bin/env python3
"""
Gemini AI Analyzer for HighTrade
Two-tier LLM analysis pipeline:
  Layer 1: Gemini 2.5 Flash  - every cycle, fast pre-analysis
  Layer 2: Gemini 3 Pro      - elevated signals only, deep analysis
"""

import json
import logging
import requests
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import gemini_client
import grok_client

logger = logging.getLogger(__name__)

FLASH_MODEL = "gemini-2.5-flash"
PRO_MODEL   = "gemini-3.1-pro-preview"
GROK_MODEL  = "grok-3"

# Trigger Pro/Grok analysis when score exceeds this
PRO_TRIGGER_SCORE = 40.0


class GrokAnalyzer:
    """X.com analysis and second opinion using xAI Grok"""

    def __init__(self, model: str = GROK_MODEL):
        self.model = model
        self.client = grok_client.GrokClient()

    def run_analysis(self, articles: List[Dict], crisis_type: str, news_score: float,
                     sector_rotation: Optional[Dict] = None,
                     vix_term_structure: Optional[Dict] = None,
                     positions: Optional[List] = None,
                     current_defcon: Optional[int] = None) -> Optional[Dict]:
        """
        Run Grok analysis for X.com sentiment and second opinion on market state.
        """
        logger.info(f"  𝕏 Running Grok analysis for X.com sentiment ({self.model})...")

        # Format snapshot payload for Grok
        payload = {
            "news_signal": {
                "score": news_score,
                "type": crisis_type,
                "articles": [a.get('title') for a in articles[:10]],
                "sentiment_summary": "bearish" # Simplification for payload
            },
            "macro": {
                "sector_rotation": sector_rotation,
                "vix_term_structure": vix_term_structure
            },
            "system_state": {
                "defcon": current_defcon,
                "positions": positions
            }
        }
        
        result = self.client.second_opinion(payload, focus=f"Market {crisis_type} event and current positions")

        if not result:
            logger.warning("  ⚠️  Grok returned no valid JSON response")
            return None

        # Map new Grok keys to internal structure
        # Internal: x_sentiment_score, trending_topics, hidden_narratives, second_opinion_action, reasoning, contrarian_signal
        # Grok Suggestions: critique (str), x_signals (list[dict]), gaps_recommendations (list[str]), action_suggestion (hold|buy|sell|monitor|add_to_watch), confidence (float 0-1)
        
        mapped_result = {
            "x_sentiment_score": result.get('confidence', 0) * (-1 if 'bearish' in result.get('critique', '').lower() else 1),
            "trending_topics": [s.get('summary') for s in result.get('x_signals', []) if isinstance(s, dict)],
            "hidden_narratives": result.get('gaps_recommendations', []),
            "second_opinion_action": result.get('action_suggestion', 'WAIT').upper(),
            "reasoning": result.get('critique', ''),
            "contrarian_signal": 1.0 - result.get('confidence', 0.5),
            "model": self.model,
            "input_tokens": result.get('input_tokens', 0),
            "output_tokens": result.get('output_tokens', 0),
            "timestamp": datetime.now().isoformat(),
            "full_critique": result # Store the full original response
        }
        
        logger.info(f"  ✅ Grok: action={mapped_result['second_opinion_action']}, x_sentiment={mapped_result['x_sentiment_score']:.2f} ({mapped_result['input_tokens']}→{mapped_result['output_tokens']} tokens)")
        return mapped_result

    def save_analysis_to_db(self, db_path: str, news_signal_id: int, analysis: Dict) -> Optional[int]:
        """Save Grok analysis to grok_analysis table"""
        import sqlite3
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO grok_analysis
                (news_signal_id, model_used, x_sentiment_score, trending_topics,
                 hidden_narratives, second_opinion_action, reasoning, 
                 input_tokens, output_tokens)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                news_signal_id,
                analysis.get('model', ''),
                analysis.get('x_sentiment_score', 0),
                json.dumps(analysis.get('trending_topics', [])),
                json.dumps(analysis.get('hidden_narratives', [])),
                analysis.get('second_opinion_action', 'WAIT'),
                analysis.get('reasoning', ''),
                analysis.get('input_tokens', 0),
                analysis.get('output_tokens', 0)
            ))
            
            conn.commit()
            analysis_id = cursor.lastrowid
            conn.close()
            return analysis_id
        except Exception as e:
            logger.error(f"  ❌ Failed to save Grok analysis: {e}")
            return None


class GeminiAnalyzer:
    """Two-tier Gemini analysis for news signals"""

    def __init__(self, api_key: str = None):
        # api_key kept for signature compatibility but ignored — gemini_client handles auth
        self.flash_model = FLASH_MODEL
        self.pro_model = PRO_MODEL
        self.pro_trigger_score = PRO_TRIGGER_SCORE

    def _parse_json_response(self, text: str) -> dict:
        """Robustly parse JSON from Gemini response, handling markdown wrapping and truncation"""
        # Strip markdown code fences
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        text = text.strip()

        # Try direct parse first
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # If truncated, attempt to repair by closing open structures
        # Find last valid closing brace position
        for end in range(len(text), 0, -1):
            candidate = text[:end]
            # Count open/close braces to find repair point
            opens = candidate.count('{') - candidate.count('}')
            if opens > 0:
                repaired = candidate.rstrip(',\n ') + ('}' * opens)
                try:
                    return json.loads(repaired)
                except json.JSONDecodeError:
                    continue

        raise ValueError(f"Could not parse JSON from response: {text[:100]}")

    def _call_gemini(self, model: str, prompt: str, temperature: float = 0.3) -> Tuple[Optional[str], int, int]:
        """
        Call Gemini via gemini_client (OAuth CLI → REST API fallback).
        model is a model_id string; we map it to the appropriate tier key.
        """
        if model == self.flash_model:
            model_key = 'flash'
        else:
            model_key = 'pro'

        return gemini_client.call(
            prompt=prompt,
            model_key=model_key,
            temperature=temperature,
        )

    def _build_flash_prompt(self, articles: List[Dict], score_components: Dict, 
                             sentiment_summary: str, crisis_type: str,
                             sector_rotation: Optional[Dict] = None,
                             vix_term_structure: Optional[Dict] = None) -> str:
        """Build prompt for Flash pre-analysis"""
        
        # Format articles: title + description (first 200 chars)
        article_lines = []
        for i, a in enumerate(articles[:30], 1):  # Max 30 articles for Flash
            title = a.get('title', '')
            desc = a.get('description', '')[:200] if a.get('description') else ''
            source = a.get('source', 'Unknown')
            pub = a.get('published_at', '')[:10]
            sentiment = a.get('sentiment', 'neutral')
            article_lines.append(f"{i}. [{source}] {title}\n   {desc}\n   Sentiment: {sentiment} | Date: {pub}")
        
        articles_text = "\n\n".join(article_lines)
        
        components_text = json.dumps(score_components, indent=2) if score_components else "{}"
        
        # Additional context from data gap fixes
        context_parts = []
        if sector_rotation:
            top = sector_rotation.get('top_sector_1w', 'N/A')
            bot = sector_rotation.get('bottom_sector_1w', 'N/A')
            context_parts.append(f"SECTOR ROTATION (1W): Top={top}, Bottom={bot}")
        
        if vix_term_structure:
            regime = vix_term_structure.get('regime', 'N/A')
            ratio = vix_term_structure.get('vix_vxv_ratio', 0)
            context_parts.append(f"VIX TERM STRUCTURE: Regime={regime}, VIX/VXV Ratio={ratio:.2f}")
            
        extra_context = "\n".join(context_parts) if context_parts else "No additional macro context available."
        
        return f"""You are a quantitative financial analyst AI. Analyze these {len(articles)} market news articles and provide a JSON response.

CURRENT SIGNAL METRICS:
- Crisis Type: {crisis_type}
- Sentiment: {sentiment_summary}
- Score Components: {components_text}

MARKET CONTEXT:
{extra_context}

NEWS ARTICLES:
{articles_text}

Respond with ONLY valid JSON in this exact structure:
{{
  "narrative_coherence": <float 0.0-1.0, how consistently do articles tell the same story>,
  "hidden_risks": [<string>, <string>, <string>],
  "contrarian_signals": "<string: what bullish or normalizing factors are being underreported>",
  "market_context": "<string: 2-3 sentence broader market context these articles suggest>",
  "confidence_in_signal": <float 0.0-1.0, is this a real market signal or just noise>,
  "dominant_theme": "<string: single most important market theme from these articles>",
  "recommended_action": "<BUY|HOLD|SELL|WAIT>",
  "reasoning": "<string: 2-3 sentence explanation of your assessment>",
  "data_gaps": ["<specific data item that was missing or stale that would have improved this analysis>", "<another gap if any — e.g. 'options flow for MSFT', 'earnings date for NVDA'>"]
}}"""

    def _build_pro_prompt(self, articles: List[Dict], score_components: Dict,
                           sentiment_summary: str, crisis_type: str, news_score: float,
                           flash_analysis: Optional[Dict], current_defcon: int,
                           positions: Optional[List] = None,
                           sector_rotation: Optional[Dict] = None,
                           vix_term_structure: Optional[Dict] = None) -> str:
        """Build deep analysis prompt for Pro model"""
        
        # All articles with full description
        article_lines = []
        for i, a in enumerate(articles, 1):
            title = a.get('title', '')
            desc = a.get('description', '')[:400] if a.get('description') else 'No description'
            source = a.get('source', 'Unknown')
            pub = a.get('published_at', '')
            sentiment = a.get('sentiment', 'neutral')
            urgency = a.get('urgency', 'routine')
            confidence = a.get('confidence', 0)
            keywords = a.get('matched_keywords', [])
            article_lines.append(
                f"{i}. [{source}] [{urgency.upper()}] {title}\n"
                f"   Published: {pub}\n"
                f"   Description: {desc}\n"
                f"   Sentiment: {sentiment} | Confidence: {confidence}/100\n"
                f"   Keywords matched: {', '.join(keywords) if keywords else 'none'}"
            )
        
        articles_text = "\n\n".join(article_lines)
        
        flash_text = json.dumps(flash_analysis, indent=2) if flash_analysis else "Not available"
        
        positions_text = ""
        if positions:
            pos_lines = [f"  - {p.get('symbol', 'N/A')}: {p.get('shares', 0)} shares @ ${p.get('entry_price', 0):.2f}, current P&L: {p.get('pnl_pct', 0):+.1f}%" for p in positions]
            positions_text = "\nCURRENT POSITIONS:\n" + "\n".join(pos_lines)

        # Macro Context
        macro_lines = []
        if sector_rotation:
            macro_lines.append("SECTOR ROTATION (Relative Strength to SPY):")
            for s in sector_rotation.get('sectors', [])[:5]:
                macro_lines.append(f"  - {s['name']} ({s['symbol']}): 1W Rel={s['rel_1w']:+.2f}%, 1M Rel={s['rel_1m']:+.2f}%")
        
        if vix_term_structure:
            macro_lines.append(f"VIX TERM STRUCTURE: {vix_term_structure['regime']} (VIX/VXV={vix_term_structure['vix_vxv_ratio']:.2f})")

        macro_text = "\n".join(macro_lines) if macro_lines else "No additional macro data available."
        
        return f"""You are a senior quantitative trading analyst with expertise in crisis detection and risk management. 
This is a DEEP ANALYSIS triggered because the news signal score ({news_score:.1f}/100) exceeded the alert threshold.

SYSTEM STATE:
- Current DEFCON Level: {current_defcon}/5 (1=highest alert, 5=normal)
- News Score: {news_score:.1f}/100
- Crisis Type: {crisis_type}
- Sentiment: {sentiment_summary}
{positions_text}

MACRO & SECTOR CONTEXT:
{macro_text}

SCORE COMPONENTS:
{json.dumps(score_components, indent=2)}

GEMINI FLASH PRE-ANALYSIS:
{flash_text}

ALL {len(articles)} NEWS ARTICLES:
{articles_text}

Provide a comprehensive trading risk analysis. Respond with ONLY valid JSON:
{{
  "narrative_coherence": <float 0.0-1.0>,
  "hidden_risks": [<string>, <string>, <string>, <string>, <string>],
  "contrarian_signals": "<detailed string>",
  "market_context": "<detailed 3-5 sentence market context>",
  "confidence_in_signal": <float 0.0-1.0>,
  "dominant_theme": "<string>",
  "recommended_action": "<BUY|HOLD|SELL|WAIT>",
  "defcon_recommendation": <int 1-5, recommended DEFCON level based on news>,
  "position_risk_assessment": "<string: assessment of risk to current positions>",
  "key_watchpoints": [<string>, <string>, <string>],
  "reasoning": "<detailed 4-6 sentence chain of thought explaining your full assessment>",
  "data_gaps": ["<specific data that was absent or stale and would have sharpened this analysis — e.g. 'options flow for AAPL', 'Fed minutes released today not in articles'>"]
}}"""

    def run_flash_analysis(self, articles: List[Dict], score_components: Dict,
                           sentiment_summary: str, crisis_type: str,
                           sector_rotation: Optional[Dict] = None,
                           vix_term_structure: Optional[Dict] = None) -> Optional[Dict]:
        """
        Run Gemini Flash analysis - called every monitoring cycle.
        Fast, cheap, enriches stored data.
        """
        logger.info(f"  🤖 Running Gemini Flash analysis ({len(articles)} articles)...")
        
        prompt = self._build_flash_prompt(
            articles, score_components, sentiment_summary, crisis_type,
            sector_rotation, vix_term_structure
        )
        
        text, input_tokens, output_tokens = self._call_gemini(self.flash_model, prompt, temperature=0.2)
        
        if not text:
            logger.warning("  ⚠️  Gemini Flash returned no response")
            return None
        
        try:
            result = self._parse_json_response(text)
            result['model'] = self.flash_model
            result['input_tokens'] = input_tokens
            result['output_tokens'] = output_tokens
            result['timestamp'] = datetime.now().isoformat()

            action = result.get('recommended_action', 'WAIT')
            coherence = result.get('narrative_coherence', 0)
            confidence = result.get('confidence_in_signal', 0)
            logger.info(f"  ✅ Flash: action={action}, coherence={coherence:.2f}, signal_confidence={confidence:.2f} ({input_tokens}→{output_tokens} tokens)")

            return result

        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"  ❌ Flash JSON parse error: {e}")
            logger.debug(f"  Raw response: {text[:300]}")
            return None

    def run_pro_analysis(self, articles: List[Dict], score_components: Dict,
                         sentiment_summary: str, crisis_type: str, news_score: float,
                         flash_analysis: Optional[Dict], current_defcon: int,
                         positions: Optional[List] = None,
                         sector_rotation: Optional[Dict] = None,
                         vix_term_structure: Optional[Dict] = None) -> Optional[Dict]:
        """
        Run Gemini Pro deep analysis - triggered on elevated signals.
        Thorough, full context, influences DEFCON decisions.
        """
        logger.info(f"  🧠 Running Gemini Pro DEEP analysis (score={news_score:.1f}, defcon={current_defcon})...")
        
        prompt = self._build_pro_prompt(
            articles, score_components, sentiment_summary, crisis_type,
            news_score, flash_analysis, current_defcon, positions,
            sector_rotation, vix_term_structure
        )
        
        # Pro gets full reasoning budget via gemini_client
        text, input_tokens, output_tokens = gemini_client.call(
            prompt=prompt,
            model_key='balanced',
        )

        if not text:
            logger.error("  ❌ Gemini Pro returned no response")
            return None

        try:
            result = self._parse_json_response(text)
            result['model'] = self.pro_model
            result['input_tokens'] = input_tokens
            result['output_tokens'] = output_tokens
            result['timestamp'] = datetime.now().isoformat()

            action = result.get('recommended_action', 'WAIT')
            defcon_rec = result.get('defcon_recommendation', current_defcon)
            logger.info(f"  ✅ Pro: action={action}, defcon_rec={defcon_rec} ({input_tokens}→{output_tokens} tokens)")

            return result

        except Exception as e:
            logger.error(f"  ❌ Gemini Pro analysis failed: {e}")
            return None

    def should_run_pro(self, news_score: float, breaking_count: int, defcon_changed: bool) -> bool:
        """Determine if Pro analysis should be triggered"""
        return (
            news_score >= self.pro_trigger_score or
            breaking_count >= 2 or
            defcon_changed
        )

    def save_analysis_to_db(self, db_path: str, news_signal_id: int,
                             analysis: Dict, trigger_type: str) -> Optional[int]:
        """Save Gemini analysis to gemini_analysis table"""
        import sqlite3
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO gemini_analysis
                (news_signal_id, model_used, trigger_type, narrative_coherence,
                 hidden_risks, contrarian_signals, market_context, confidence_in_signal,
                 recommended_action, reasoning, input_tokens, output_tokens)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                news_signal_id,
                analysis.get('model', ''),
                trigger_type,
                analysis.get('narrative_coherence', 0),
                json.dumps(analysis.get('hidden_risks', [])),
                analysis.get('contrarian_signals', ''),
                analysis.get('market_context', ''),
                analysis.get('confidence_in_signal', 0),
                analysis.get('recommended_action', 'WAIT'),
                analysis.get('reasoning', ''),
                analysis.get('input_tokens', 0),
                analysis.get('output_tokens', 0)
            ))
            
            conn.commit()
            analysis_id = cursor.lastrowid
            conn.close()
            
            logger.debug(f"  💾 Saved Gemini analysis ID={analysis_id}")
            return analysis_id
            
        except Exception as e:
            logger.error(f"  ❌ Failed to save Gemini analysis: {e}")
            return None


if __name__ == '__main__':
    # Quick test
    import sys
    print("Testing Gemini Analyzer...")
    
    analyzer = GeminiAnalyzer()
    
    test_articles = [
        {
            'title': 'Fed signals emergency rate consideration amid inflation surge',
            'description': 'Federal Reserve officials are discussing emergency measures as inflation hits 7.2%, the highest in 40 years. Markets reacting sharply.',
            'source': 'Bloomberg',
            'published_at': datetime.now().isoformat(),
            'sentiment': 'bearish',
            'urgency': 'high',
            'confidence': 75,
            'matched_keywords': ['inflation', 'emergency', 'rate']
        },
        {
            'title': 'Treasury yields spike to 20-year high on Fed speculation',
            'description': 'Bond markets in turmoil as yields on 10-year treasuries hit levels not seen since 2004. Credit spreads widening.',
            'source': 'Reuters',
            'published_at': datetime.now().isoformat(),
            'sentiment': 'bearish',
            'urgency': 'high',
            'confidence': 80,
            'matched_keywords': ['yield', 'crisis', 'credit']
        }
    ]
    
    result = analyzer.run_flash_analysis(
        test_articles,
        score_components={'sentiment_net': 65, 'signal_concentration': 80},
        sentiment_summary='Bearish: 60%, Bullish: 10%, Neutral: 30%',
        crisis_type='inflation_rate'
    )
    
    if result:
        print(f"✅ Flash analysis successful!")
        print(f"   Action: {result.get('recommended_action')}")
        print(f"   Coherence: {result.get('narrative_coherence')}")
        print(f"   Signal confidence: {result.get('confidence_in_signal')}")
        print(f"   Reasoning: {result.get('reasoning', '')[:150]}...")
    else:
        print("❌ Flash analysis failed")
        sys.exit(1)
