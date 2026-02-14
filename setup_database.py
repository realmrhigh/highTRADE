#!/usr/bin/env python3
"""
Database initialization script for TradingBot
Creates SQLite database with all required tables and indexes
Run this ONCE to set up the database structure
"""

import sqlite3
import os
from datetime import datetime

def setup_database(db_path: str = "~/trading_data/trading_history.db"):
    """Initialize the TradingBot database"""

    # Expand home directory
    db_path = os.path.expanduser(db_path)

    # Create directory if it doesn't exist
    db_dir = os.path.dirname(db_path)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
        print(f"✓ Created directory: {db_dir}")

    # Check if database already exists
    db_exists = os.path.exists(db_path)

    # Connect to database
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # ===== TABLE 1: market_crises =====
    print("\n📊 Creating market_crises table...")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS market_crises (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            event_type TEXT NOT NULL,
            trigger_description TEXT,
            drawdown_percent REAL,
            recovery_days INTEGER,
            signals JSON,
            resolution_catalyst TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    print("   ✓ market_crises table ready")

    # ===== TABLE 2: market_signals =====
    print("\n🚨 Creating market_signals table...")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS market_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            signal_type TEXT NOT NULL,
            confidence REAL,
            context JSON,
            defcon_level INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    print("   ✓ market_signals table ready")

    # ===== TABLE 3: signal_history =====
    print("\n📈 Creating signal_history table...")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS signal_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id INTEGER,
            crisis_id INTEGER,
            lead_time_days INTEGER,
            accuracy REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (signal_id) REFERENCES market_signals(id),
            FOREIGN KEY (crisis_id) REFERENCES market_crises(id)
        )
    """)
    print("   ✓ signal_history table ready")

    # ===== INDEXES =====
    print("\n🔍 Creating indexes for performance...")

    # Index on crisis date for sorting
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_crises_date
        ON market_crises(date DESC)
    """)
    print("   ✓ idx_crises_date")

    # Index on crisis event_type for filtering
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_crises_event_type
        ON market_crises(event_type)
    """)
    print("   ✓ idx_crises_event_type")

    # Index on signal timestamp
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_signals_timestamp
        ON market_signals(timestamp DESC)
    """)
    print("   ✓ idx_signals_timestamp")

    # Index on signal type
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_signals_type
        ON market_signals(signal_type)
    """)
    print("   ✓ idx_signals_type")

    # Index on DEFCON level
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_signals_defcon
        ON market_signals(defcon_level)
    """)
    print("   ✓ idx_signals_defcon")

    # Composite index for crisis queries
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_crises_date_type
        ON market_crises(date DESC, event_type)
    """)
    print("   ✓ idx_crises_date_type")

    # Commit changes
    conn.commit()

    # Get database stats
    cur.execute("SELECT COUNT(*) FROM market_crises")
    crisis_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM market_signals")
    signal_count = cur.fetchone()[0]

    conn.close()

    # Print summary
    print("\n" + "="*60)
    print("✅ DATABASE SETUP COMPLETE")
    print("="*60)
    print(f"\n📁 Database: {db_path}")
    print(f"💾 Size: {os.path.getsize(db_path) / 1024:.1f} KB")
    print(f"\n📊 Tables created:")
    print(f"   • market_crises ({crisis_count} records)")
    print(f"   • market_signals ({signal_count} records)")
    print(f"   • signal_history")
    print(f"\n🔍 Indexes created (6 total)")
    print(f"\n{'NEW DATABASE' if not db_exists else 'UPDATED DATABASE'}")
    print("\n✨ Ready to populate with crisis data!")
    print("\n💡 Next steps:")
    print("   1. Copy add_crisis_template.py to add_YOUR_CRISIS.py")
    print("   2. Edit with your crisis data")
    print("   3. Run: python add_YOUR_CRISIS.py")
    print("\n" + "="*60)


if __name__ == "__main__":
    print("\n🚀 TradingBot Database Initialization")
    print("="*60)

    try:
        setup_database()
    except Exception as e:
        print(f"\n❌ Error: {e}")
        exit(1)
