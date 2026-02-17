#!/opt/homebrew/bin/python3.11
"""
HighTrade MCP Server
Exposes trading system to Claude Desktop for remote monitoring and control
"""

import sys
import json
import sqlite3
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional

# MCP SDK imports
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent
except ImportError:
    print("ERROR: MCP SDK not installed. Install with: pip install mcp")
    sys.exit(1)

# Configuration
# Use script directory for database paths
SCRIPT_DIR = Path(__file__).parent.resolve()
DB_PATH = SCRIPT_DIR / 'trading_data' / 'trading_history.db'
COMMAND_DB = SCRIPT_DIR / 'trading_data' / 'mcp_commands.db'

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class HighTradeMCPServer:
    """MCP Server for HighTrade system"""

    def __init__(self):
        self.server = Server("hightrade")
        self._init_command_db()
        self._setup_tools()

    def _init_command_db(self):
        """Initialize command database for IPC"""
        conn = sqlite3.connect(str(COMMAND_DB))
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS mcp_commands (
                command_id INTEGER PRIMARY KEY AUTOINCREMENT,
                command_type TEXT,
                command_data TEXT,
                status TEXT DEFAULT 'pending',
                response TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP
            )
        """)

        conn.commit()
        conn.close()
        logger.info("Command database initialized")

    def _setup_tools(self):
        """Register all MCP tools"""

        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            """List available tools"""
            return [
                Tool(
                    name="get_system_status",
                    description="Get current trading system status including DEFCON level, positions, P&L, and broker mode",
                    inputSchema={
                        "type": "object",
                        "properties": {},
                    }
                ),
                Tool(
                    name="get_recent_signals",
                    description="Get recent market and news signals with timestamps",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "limit": {
                                "type": "number",
                                "description": "Number of signals to retrieve (default: 10)",
                                "default": 10
                            }
                        },
                    }
                ),
                Tool(
                    name="get_recent_news",
                    description="Get recent news signals and crisis alerts with full article data",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "limit": {
                                "type": "number",
                                "description": "Number of news signals to retrieve (default: 10)",
                                "default": 10
                            }
                        },
                    }
                ),
                Tool(
                    name="submit_claude_analysis",
                    description="Submit enhanced news analysis from Claude to influence trading decisions",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "news_signal_id": {"type": "number"},
                            "enhanced_confidence": {"type": "number"},
                            "sentiment_override": {"type": "string", "enum": ["bearish", "bullish", "neutral"]},
                            "reasoning": {"type": "string"},
                            "recommended_action": {"type": "string", "enum": ["BUY", "HOLD", "SELL", "WAIT"]},
                            "risk_factors": {"type": "array", "items": {"type": "string"}},
                            "opportunity_score": {"type": "number"},
                            "narrative_coherence": {"type": "number"},
                            "sources_verified": {"type": "number"}
                        },
                        "required": ["news_signal_id", "enhanced_confidence", "reasoning"]
                    }
                ),
                Tool(
                    name="get_article_details",
                    description="Get full article details for a specific news signal",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "news_signal_id": {"type": "number"}
                        },
                        "required": ["news_signal_id"]
                    }
                ),
                Tool(
                    name="get_system_architecture",
                    description="Get system architecture and human-in-the-loop safeguards",
                    inputSchema={
                        "type": "object",
                        "properties": {}
                    }
                ),
            ]

        @self.server.call_tool()
        async def call_tool(name: str, arguments: dict) -> list[TextContent]:
            """Handle tool calls"""
            try:
                if name == "get_system_status":
                    result = self._get_system_status()
                elif name == "get_recent_signals":
                    limit = arguments.get("limit", 10)
                    result = self._get_recent_signals(limit)
                elif name == "get_recent_news":
                    limit = arguments.get("limit", 10)
                    result = self._get_recent_news(limit)
                elif name == "submit_claude_analysis":
                    result = self._submit_claude_analysis(arguments)
                elif name == "get_article_details":
                    result = self._get_article_details(arguments.get("news_signal_id"))
                elif name == "get_system_architecture":
                    result = self._get_system_architecture()
                else:
                    result = {"error": f"Unknown tool: {name}"}

                return [TextContent(
                    type="text",
                    text=json.dumps(result, indent=2)
                )]

            except Exception as e:
                logger.error(f"Error calling tool {name}: {e}", exc_info=True)
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": str(e)})
                )]

    def _get_system_status(self) -> Dict:
        """Get current system status"""
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.cursor()

            # Get latest monitoring point
            cursor.execute("""
                SELECT monitoring_date, monitoring_time, defcon_level, signal_score,
                       bond_10yr_yield, vix_close, news_score
                FROM signal_monitoring
                ORDER BY monitoring_date DESC, monitoring_time DESC
                LIMIT 1
            """)

            row = cursor.fetchone()
            if row:
                status = {
                    "timestamp": f"{row[0]} {row[1]}",
                    "defcon_level": row[2],
                    "signal_score": round(row[3], 1) if row[3] else 0,
                    "bond_yield": round(row[4], 2) if row[4] else None,
                    "vix": round(row[5], 2) if row[5] else None,
                    "news_score": round(row[6], 1) if row[6] else 0,
                }
            else:
                status = {"error": "No monitoring data available"}

            # Get total P&L
            cursor.execute("""
                SELECT SUM(profit_loss_dollars)
                FROM trade_records
                WHERE exit_date IS NOT NULL
            """)
            total_pnl = cursor.fetchone()[0] or 0

            status["total_pnl"] = round(total_pnl, 2)

            conn.close()
            return status

        except Exception as e:
            logger.error(f"Error getting system status: {e}")
            return {"error": str(e)}

    def _get_recent_signals(self, limit: int = 10) -> Dict:
        """Get recent market signals"""
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.cursor()

            cursor.execute("""
                SELECT monitoring_date, monitoring_time, defcon_level, signal_score,
                       bond_10yr_yield, vix_close, news_score
                FROM signal_monitoring
                ORDER BY monitoring_date DESC, monitoring_time DESC
                LIMIT ?
            """, (limit,))

            signals = []
            for row in cursor.fetchall():
                signals.append({
                    "timestamp": f"{row[0]} {row[1]}",
                    "defcon": row[2],
                    "signal_score": round(row[3], 1) if row[3] else 0,
                    "bond_yield": round(row[4], 2) if row[4] else None,
                    "vix": round(row[5], 2) if row[5] else None,
                    "news_score": round(row[6], 1) if row[6] else 0,
                })

            conn.close()
            return {"signals": signals, "count": len(signals)}

        except Exception as e:
            logger.error(f"Error getting signals: {e}")
            return {"error": str(e)}

    def _get_recent_news(self, limit: int = 10) -> Dict:
        """Get recent news signals with full scoring context and Gemini analysis"""
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.cursor()

            cursor.execute("""
                SELECT news_signal_id, timestamp, news_score, dominant_crisis_type,
                       crisis_description, breaking_news_override, recommended_defcon,
                       article_count, breaking_count, avg_confidence, sentiment_summary,
                       sentiment_net_score, signal_concentration,
                       crisis_distribution_json, score_components_json, keyword_hits_json
                FROM news_signals
                ORDER BY timestamp DESC
                LIMIT ?
            """, (limit,))

            def safe_json(val):
                try:
                    return json.loads(val) if val else None
                except Exception:
                    return None

            news_signals = []
            for row in cursor.fetchall():
                signal_id = row[0]

                # Check if any Gemini Pro analysis exists for this signal
                cursor2 = conn.cursor()
                cursor2.execute("""
                    SELECT recommended_action, confidence_in_signal, reasoning
                    FROM gemini_analysis WHERE news_signal_id = ?
                    ORDER BY created_at DESC LIMIT 1
                """, (signal_id,))
                gemini_row = cursor2.fetchone()

                entry = {
                    "news_signal_id": signal_id,
                    "timestamp": row[1],
                    "news_score": round(row[2], 2) if row[2] else 0,
                    "crisis_type": row[3],
                    "description": row[4],
                    "breaking_override": bool(row[5]),
                    "recommended_defcon": row[6],
                    "article_count": row[7],
                    "breaking_count": row[8],
                    "avg_confidence": round(row[9], 1) if row[9] else 0,
                    "sentiment": row[10],
                    "sentiment_net_score": row[11],
                    "signal_concentration": row[12],
                    "crisis_distribution": safe_json(row[13]),
                    "score_components": safe_json(row[14]),
                    "keyword_hits": safe_json(row[15]),
                    "gemini_pro_action": gemini_row[0] if gemini_row else None,
                    "gemini_pro_confidence": gemini_row[1] if gemini_row else None,
                    "gemini_pro_reasoning": gemini_row[2][:200] if gemini_row and gemini_row[2] else None
                }
                news_signals.append(entry)

            conn.close()
            return {
                "news": news_signals,
                "count": len(news_signals),
                "tip": "Use get_article_details(news_signal_id) for full articles + Gemini analyses"
            }

        except Exception as e:
            logger.error(f"Error getting news: {e}")
            return {"error": str(e)}

    def _submit_claude_analysis(self, args: Dict) -> Dict:
        """Submit Claude's enhanced analysis"""
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO claude_analysis
                (news_signal_id, enhanced_confidence, sentiment_override, reasoning,
                 recommended_action, risk_factors, opportunity_score, narrative_coherence,
                 sources_verified, analysis_timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                args.get("news_signal_id"),
                args.get("enhanced_confidence"),
                args.get("sentiment_override"),
                args.get("reasoning"),
                args.get("recommended_action"),
                json.dumps(args.get("risk_factors", [])),
                args.get("opportunity_score"),
                args.get("narrative_coherence"),
                args.get("sources_verified"),
                datetime.now().isoformat()
            ))
            
            conn.commit()
            analysis_id = cursor.lastrowid
            conn.close()
            
            return {
                "success": True,
                "analysis_id": analysis_id,
                "message": "Analysis submitted successfully"
            }
            
        except Exception as e:
            logger.error(f"Error submitting analysis: {e}")
            return {"error": str(e)}
    
    def _get_article_details(self, news_signal_id: int) -> Dict:
        """Get full article details for a news signal including Gemini analyses"""
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.cursor()

            cursor.execute("""
                SELECT news_signal_id, timestamp, news_score, dominant_crisis_type,
                       crisis_description, breaking_news_override, recommended_defcon,
                       article_count, breaking_count, avg_confidence, sentiment_summary,
                       sentiment_net_score, signal_concentration,
                       crisis_distribution_json, score_components_json,
                       keyword_hits_json, articles_full_json, gemini_flash_json
                FROM news_signals
                WHERE news_signal_id = ?
            """, (news_signal_id,))

            row = cursor.fetchone()
            if not row:
                conn.close()
                return {"error": f"News signal {news_signal_id} not found"}

            # Parse JSON fields safely
            def safe_json(val):
                try:
                    return json.loads(val) if val else None
                except Exception:
                    return None

            result = {
                "news_signal_id": row[0],
                "timestamp": row[1],
                "news_score": round(row[2], 2) if row[2] else 0,
                "crisis_type": row[3],
                "description": row[4],
                "breaking_override": bool(row[5]),
                "recommended_defcon": row[6],
                "article_count": row[7],
                "breaking_count": row[8],
                "avg_confidence": round(row[9], 1) if row[9] else 0,
                "sentiment_summary": row[10],
                "sentiment_net_score": row[11],
                "signal_concentration": row[12],
                "crisis_distribution": safe_json(row[13]),
                "score_components": safe_json(row[14]),
                "keyword_hits": safe_json(row[15]),
                "articles": safe_json(row[16]),   # ALL articles with full descriptions
                "gemini_flash_analysis": safe_json(row[17])
            }

            # Fetch any Gemini Pro analyses linked to this signal
            cursor.execute("""
                SELECT model_used, trigger_type, narrative_coherence, hidden_risks,
                       contrarian_signals, market_context, confidence_in_signal,
                       recommended_action, reasoning, input_tokens, output_tokens, created_at
                FROM gemini_analysis
                WHERE news_signal_id = ?
                ORDER BY created_at DESC
            """, (news_signal_id,))

            pro_rows = cursor.fetchall()
            if pro_rows:
                result["gemini_pro_analyses"] = [
                    {
                        "model": r[0],
                        "trigger_type": r[1],
                        "narrative_coherence": r[2],
                        "hidden_risks": safe_json(r[3]),
                        "contrarian_signals": r[4],
                        "market_context": r[5],
                        "confidence_in_signal": r[6],
                        "recommended_action": r[7],
                        "reasoning": r[8],
                        "tokens": {"input": r[9], "output": r[10]},
                        "created_at": r[11]
                    }
                    for r in pro_rows
                ]

            conn.close()
            return result

        except Exception as e:
            logger.error(f"Error getting article details: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return {"error": str(e)}
    
    def _get_system_architecture(self) -> Dict:
        """Get system architecture info"""
        return {
            "system": "HighTrade Autonomous Trading System",
            "version": "Phase 6 - Claude Feedback Loop",
            "human_in_loop_safeguards": [
                "Paper trading mode only (no real money)",
                "Manual approval required for all trades",
                "DEFCON-based position sizing (conservative)",
                "Stop-loss and profit targets enforced",
                "Maximum portfolio exposure limits",
                "Crisis event validation"
            ],
            "components": {
                "monitoring": "Real-time DEFCON and signal scoring",
                "news_analysis": "Multi-source news aggregation and sentiment",
                "claude_feedback": "Enhanced analysis and confidence adjustments",
                "paper_trading": "Simulated trade execution and tracking",
                "mcp_server": "Remote monitoring via Claude Desktop"
            }
        }

    async def run(self):
        """Run the MCP server"""
        async with stdio_server() as (read_stream, write_stream):
            await self.server.run(
                read_stream,
                write_stream,
                self.server.create_initialization_options()
            )


# Main entry point
async def main():
    logger.info("Starting HighTrade MCP Server...")
    server = HighTradeMCPServer()
    await server.run()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
