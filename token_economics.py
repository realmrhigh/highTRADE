#!/usr/bin/env python3
"""
Token Economics & Self-Funding Engine
Tracks API token burn vs Realized P&L.
If we make money -> we buy more API compute.
If we lose money -> we throttle background thinking.
"""

import sqlite3
from trading_db import get_sqlite_conn
import json
import logging
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)
SCRIPT_DIR = Path(__file__).parent.resolve()
DB_PATH = SCRIPT_DIR / 'trading_data' / 'trading_history.db'

# Estimated costs per 1M tokens (USD)
COSTS = {
    'gemini-2.5-pro': {'input': 1.25, 'output': 5.00},
    'gemini-3.1-pro-preview': {'input': 2.50, 'output': 10.00},
    'grok-4-1-fast-reasoning': {'input': 2.00, 'output': 10.00},
}

class TokenEconomics:
    def __init__(self):
        self._init_db()

    def _init_db(self):
        conn = get_sqlite_conn(str(DB_PATH))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS token_burn_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                model TEXT,
                caller TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                estimated_cost_usd REAL
            )
        """)
        conn.commit()
        conn.close()

    def log_usage(self, model: str, caller: str, in_tok: int, out_tok: int):
        rates = COSTS.get(model, {'input': 0, 'output': 0})
        cost = (in_tok / 1_000_000 * rates['input']) + (out_tok / 1_000_000 * rates['output'])
        
        conn = get_sqlite_conn(str(DB_PATH))
        conn.execute(
            "INSERT INTO token_burn_log (model, caller, input_tokens, output_tokens, estimated_cost_usd) VALUES (?, ?, ?, ?, ?)",
            (model, caller, in_tok, out_tok, cost)
        )
        conn.commit()
        conn.close()
        return cost

    def get_monthly_stats(self):
        conn = get_sqlite_conn(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        
        # Token costs
        cost_row = conn.execute("""
            SELECT SUM(estimated_cost_usd) as total_cost 
            FROM token_burn_log 
            WHERE timestamp >= datetime('now', '-30 days')
        """).fetchone()
        total_cost = cost_row['total_cost'] or 0.0

        # P&L
        pnl_row = conn.execute("""
            SELECT SUM(profit_loss_dollars) as total_pnl 
            FROM trade_records 
            WHERE status = 'closed' AND exit_date >= datetime('now', '-30 days')
        """).fetchone()
        
        total_pnl = pnl_row['total_pnl'] or 0.0
        conn.close()
        
        roi = total_pnl - total_cost
        
        return {
            "api_spend_30d": total_cost,
            "realized_pnl_30d": total_pnl,
            "net_roi": roi,
            "is_profitable": roi > 0
        }

    def evaluate_budget_proposal(self):
        stats = self.get_monthly_stats()
        if stats['is_profitable'] and stats['realized_pnl_30d'] > stats['api_spend_30d'] * 5:
            return f"PROPOSAL: We made ${stats['realized_pnl_30d']:.2f} this month on a ${stats['api_spend_30d']:.2f} API budget. Increase token allowance by 50% for deeper reasoning loops."
        elif not stats['is_profitable']:
            return f"CRITIQUE: We are bleeding (${stats['realized_pnl_30d']:.2f} P&L vs ${stats['api_spend_30d']:.2f} spend). Throttling proactive research loops until win rate improves."
        else:
            return f"STATUS: Net positive but tight margin (Net: ${stats['net_roi']:.2f}). Holding current API limits."

if __name__ == "__main__":
    te = TokenEconomics()
    print("Token Economics Engine Initialized.")
    print(te.evaluate_budget_proposal())
