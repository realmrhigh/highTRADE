#!/usr/bin/env python3
"""
Database migration script to add paper trading features
"""

import sqlite3
from pathlib import Path

DB_PATH = Path.home() / 'trading_data' / 'trading_history.db'

def migrate_database():
    """Add necessary columns for paper trading"""
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()

    print("Starting database migration...\n")

    # 1. Add asset_symbol to trade_records if not exists
    try:
        cursor.execute("ALTER TABLE trade_records ADD COLUMN asset_symbol TEXT")
        print("✅ Added asset_symbol column to trade_records")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e):
            print("⚠️  asset_symbol column already exists")
        else:
            raise

    # 2. Add status column if not exists
    try:
        cursor.execute("ALTER TABLE trade_records ADD COLUMN status TEXT DEFAULT 'closed' CHECK(status IN ('open', 'closed'))")
        print("✅ Added status column to trade_records")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e):
            print("⚠️  status column already exists")
        else:
            raise

    # 3. Update crisis_events table to support 'signal' category
    try:
        # Check if 'signal' is in the category check constraint
        cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='crisis_events'")
        schema = cursor.fetchone()[0]
        if "'signal'" not in schema:
            # Need to recreate the table with updated constraint
            print("⏳ Updating crisis_events table to support 'signal' category...")
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS crisis_events_new (
                crisis_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT,
                trigger TEXT,
                start_date TEXT NOT NULL,
                crisis_bottom_date TEXT,
                recovery_date TEXT,
                resolution_announcement_date TEXT,
                market_drop_percent REAL,
                recovery_percent REAL,
                recovery_days INTEGER,
                severity TEXT CHECK(severity IN ('minor', 'moderate', 'severe')),
                category TEXT CHECK(category IN ('trade', 'policy', 'geopolitical', 'financial', 'epidemic', 'signal')),
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')
            cursor.execute('INSERT INTO crisis_events_new SELECT * FROM crisis_events')
            cursor.execute('DROP TABLE crisis_events')
            cursor.execute('ALTER TABLE crisis_events_new RENAME TO crisis_events')
            print("✅ Updated crisis_events table to support 'signal' category")
    except Exception as e:
        print(f"⚠️  Note: {e}")

    conn.commit()
    print("\n✅ Database migration complete!")
    conn.close()

if __name__ == '__main__':
    migrate_database()
