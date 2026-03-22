#!/usr/bin/env python3
"""
Add news_signals table and update signal_monitoring for news integration
"""

import sqlite3
from trading_db import get_sqlite_conn
from pathlib import Path

# Use correct path relative to script location
SCRIPT_DIR = Path(__file__).parent.resolve()
DB_PATH = SCRIPT_DIR / 'trading_data' / 'trading_history.db'

def add_news_signals_table():
    """Add news_signals table for storing news analysis results"""
    conn = get_sqlite_conn(str(DB_PATH))
    cursor = conn.cursor()

    # Create news_signals table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS news_signals (
        news_signal_id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        news_score REAL,
        dominant_crisis_type TEXT,
        crisis_description TEXT,
        breaking_news_override BOOLEAN,
        recommended_defcon INTEGER,
        article_count INTEGER,
        breaking_count INTEGER,
        avg_confidence REAL,
        sentiment_summary TEXT,
        articles_json TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    print("✓ Created news_signals table")

    # Add news_score column to signal_monitoring if it doesn't exist
    try:
        cursor.execute('ALTER TABLE signal_monitoring ADD COLUMN news_score REAL DEFAULT 0')
        print("✓ Added news_score column to signal_monitoring")
    except sqlite3.OperationalError:
        print("  Note: news_score column already exists in signal_monitoring")

    # Add composite_signal_score column to signal_monitoring if it doesn't exist
    try:
        cursor.execute('ALTER TABLE signal_monitoring ADD COLUMN composite_signal_score REAL')
        print("✓ Added composite_signal_score column to signal_monitoring")
    except sqlite3.OperationalError:
        print("  Note: composite_signal_score column already exists in signal_monitoring")

    conn.commit()
    conn.close()

    print(f"\n✓ Database schema updated successfully")
    print(f"Database location: {DB_PATH}")

if __name__ == '__main__':
    print("🔧 Adding news schema to HighTrade database\n")
    add_news_signals_table()
