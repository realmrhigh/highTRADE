#!/usr/bin/env python3
"""
Crisis Pattern Storage Utilities
Manage market crisis data compilation and queries
"""

import sqlite3
import json
import os
from datetime import datetime
from typing import List, Dict, Any

class CrisisDatabase:
    def __init__(self, db_path: str = "~/trading_data/trading_history.db"):
        self.db_path = os.path.expanduser(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.cur = self.conn.cursor()

    def add_crisis(self, crisis_data: Dict[str, Any]) -> int:
        """Add a new market crisis to the database"""
        self.cur.execute("""
            INSERT INTO market_crises
            (date, event_type, trigger_description, drawdown_percent,
             recovery_days, signals, resolution_catalyst)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            crisis_data.get("date"),
            crisis_data.get("event_type"),
            crisis_data.get("trigger_description"),
            crisis_data.get("drawdown_percent"),
            crisis_data.get("recovery_days"),
            json.dumps(crisis_data.get("signals", {})),
            crisis_data.get("resolution_catalyst")
        ))
        self.conn.commit()
        return self.cur.lastrowid

    def add_signal(self, signal_data: Dict[str, Any]) -> int:
        """Add a real-time market signal"""
        self.cur.execute("""
            INSERT INTO market_signals
            (signal_type, confidence, context, defcon_level)
            VALUES (?, ?, ?, ?)
        """, (
            signal_data.get("signal_type"),
            signal_data.get("confidence"),
            json.dumps(signal_data.get("context", {})),
            signal_data.get("defcon_level")
        ))
        self.conn.commit()
        return self.cur.lastrowid

    def get_all_crises(self) -> List[Dict[str, Any]]:
        """Retrieve all crises"""
        self.cur.execute("SELECT * FROM market_crises ORDER BY date DESC;")
        return self._format_crises(self.cur.fetchall())

    def get_crisis_by_type(self, event_type: str) -> List[Dict[str, Any]]:
        """Get crises by event type"""
        self.cur.execute(
            "SELECT * FROM market_crises WHERE event_type = ? ORDER BY date DESC;",
            (event_type,)
        )
        return self._format_crises(self.cur.fetchall())

    def get_recent_signals(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent market signals"""
        self.cur.execute(
            "SELECT * FROM market_signals ORDER BY timestamp DESC LIMIT ?;",
            (limit,)
        )
        rows = self.cur.fetchall()
        return [
            {
                "id": r[0],
                "timestamp": r[1],
                "signal_type": r[2],
                "confidence": r[3],
                "context": json.loads(r[4]),
                "defcon_level": r[5]
            }
            for r in rows
        ]

    def get_crisis_count(self) -> int:
        """Total number of crises stored"""
        self.cur.execute("SELECT COUNT(*) FROM market_crises;")
        return self.cur.fetchone()[0]

    def _format_crises(self, rows) -> List[Dict[str, Any]]:
        """Format crisis rows into dicts"""
        return [
            {
                "id": r[0],
                "date": r[1],
                "event_type": r[2],
                "trigger_description": r[3],
                "drawdown_percent": r[4],
                "recovery_days": r[5],
                "signals": json.loads(r[6]),
                "resolution_catalyst": r[7],
                "created_at": r[8]
            }
            for r in rows
        ]

    def close(self):
        """Close database connection"""
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


# Example usage
if __name__ == "__main__":
    with CrisisDatabase() as db:
        print(f"Total crises in database: {db.get_crisis_count()}")

        crises = db.get_all_crises()
        for crisis in crises:
            print(f"\n{crisis['date']} - {crisis['event_type']}")
            print(f"  Drawdown: {crisis['drawdown_percent']}%")
            print(f"  Recovery: {crisis['recovery_days']} days")
