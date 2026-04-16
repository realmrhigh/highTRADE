#!/usr/bin/env python3
"""
Migration: Add sell attempt tracking to trade_records to prevent infinite retry loops.
"""

import sqlite3
import os
import sys
from pathlib import Path

# Add current dir to path for imports
sys.path.append(str(Path(__file__).parent))
from trading_db import get_sqlite_conn
from db_paths import DB_PATH

def migrate():
    print(f"Connecting to {DB_PATH}...")
    conn = get_sqlite_conn(str(DB_PATH))
    cursor = conn.cursor()

    columns = [
        ("last_exit_attempt", "TEXT"),
        ("exit_attempt_count", "INTEGER DEFAULT 0"),
        ("exit_attempt_error", "TEXT")
    ]

    for col_name, col_type in columns:
        try:
            cursor.execute(f"ALTER TABLE trade_records ADD COLUMN {col_name} {col_type}")
            print(f"✅ Added {col_name} column")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e):
                print(f"⚠️  {col_name} already exists")
            else:
                print(f"❌ Error adding {col_name}: {e}")

    conn.commit()
    conn.close()
    print("Migration complete.")

if __name__ == "__main__":
    migrate()
