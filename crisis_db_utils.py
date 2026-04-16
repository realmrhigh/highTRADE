#!/usr/bin/env python3
"""
Crisis Pattern Storage Utilities
Manage market crisis data compilation and queries
"""

import json
from pathlib import Path
from typing import List, Dict, Any

from db_paths import DB_PATH
from trading_db import db


class CrisisDatabase:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = str(db_path)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def add_crisis(self, crisis_data: Dict[str, Any]) -> int:
        """Add a new market crisis to the database"""
        with db(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
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
            return cur.lastrowid

    def add_signal(self, signal_data: Dict[str, Any]) -> int:
        """Add a real-time market signal"""
        with db(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO market_signals
                (signal_type, confidence, context, defcon_level)
                VALUES (?, ?, ?, ?)
            """, (
                signal_data.get("signal_type"),
                signal_data.get("confidence"),
                json.dumps(signal_data.get("context", {})),
                signal_data.get("defcon_level")
            ))
            return cur.lastrowid

    def get_all_crises(self) -> List[Dict[str, Any]]:
        """Retrieve all crises"""
        with db(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM market_crises ORDER BY date DESC;")
            return self._format_crises(cur.fetchall())

    def get_crisis_by_type(self, event_type: str) -> List[Dict[str, Any]]:
        """Get crises by event type"""
        with db(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM market_crises WHERE event_type = ? ORDER BY date DESC;",
                (event_type,)
            )
            return self._format_crises(cur.fetchall())

    def get_recent_signals(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent market signals"""
        with db(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM market_signals ORDER BY timestamp DESC LIMIT ?;",
                (limit,)
            )
            rows = cur.fetchall()
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
        with db(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM market_crises;")
            return cur.fetchone()[0]

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


# Example usage
if __name__ == "__main__":
    with CrisisDatabase() as crisis_db:
        print(f"Total crises in database: {crisis_db.get_crisis_count()}")

        crises = crisis_db.get_all_crises()
        for crisis in crises:
            print(f"\n{crisis['date']} - {crisis['event_type']}")
            print(f"  Drawdown: {crisis['drawdown_percent']}%")
            print(f"  Recovery: {crisis['recovery_days']} days")
