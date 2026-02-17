#!/usr/bin/env python3
"""
Alternative Data Schema Migration
Adds tables for congressional trading and FRED macro data.

Tables added:
  - congressional_trades: House/Senate stock disclosures
  - congressional_cluster_signals: Cluster buy/sell detections
  - macro_indicators: FRED macroeconomic series snapshots

Idempotent: safe to run multiple times.
"""

import sqlite3
from pathlib import Path
from datetime import datetime

SCRIPT_DIR = Path(__file__).parent.resolve()
DB_PATH = SCRIPT_DIR / 'trading_data' / 'trading_history.db'


def _column_exists(cursor, table: str, column: str) -> bool:
    """Check if a column exists in a table"""
    cursor.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cursor.fetchall())


def _table_exists(cursor, table: str) -> bool:
    """Check if a table exists"""
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cursor.fetchone() is not None


def migrate_alt_data_schema():
    """Add alternative data tables to HighTrade database"""

    print(f"🔧 Alt Data Schema Migration")
    print(f"   Database: {DB_PATH}")
    print(f"   Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    if not DB_PATH.exists():
        print(f"❌ Database not found: {DB_PATH}")
        print("   Run setup_database.py first")
        return False

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    cursor = conn.cursor()

    # ─────────────────────────────────────────────
    # 1. congressional_trades
    # ─────────────────────────────────────────────
    if not _table_exists(cursor, 'congressional_trades'):
        cursor.execute('''
        CREATE TABLE congressional_trades (
            trade_id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source            TEXT NOT NULL,           -- 'house' | 'senate' | 'sec_form4'
            politician        TEXT NOT NULL,
            party             TEXT,                    -- 'D' | 'R' | 'I'
            ticker            TEXT NOT NULL,
            direction         TEXT,                    -- 'buy' | 'sell' | 'unknown'
            amount            REAL,                    -- estimated USD midpoint
            disclosure_date   TEXT,                   -- YYYY-MM-DD
            transaction_date  TEXT,                   -- YYYY-MM-DD (when trade occurred)
            asset_description TEXT,
            district          TEXT,                    -- state or district
            committee_hint    TEXT,                   -- relevant committee if known
            created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(politician, ticker, direction, transaction_date)
        )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_cong_trades_ticker ON congressional_trades(ticker)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_cong_trades_date ON congressional_trades(disclosure_date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_cong_trades_politician ON congressional_trades(politician)')
        print("✓ Created congressional_trades table")
    else:
        print("  ℹ️  congressional_trades already exists")

        # Add any missing columns
        new_columns = [
            ('committee_hint', 'TEXT'),
            ('district', 'TEXT'),
            ('asset_description', 'TEXT'),
        ]
        for col_name, col_type in new_columns:
            if not _column_exists(cursor, 'congressional_trades', col_name):
                cursor.execute(f'ALTER TABLE congressional_trades ADD COLUMN {col_name} {col_type}')
                print(f"  ✓ Added {col_name} to congressional_trades")

    # ─────────────────────────────────────────────
    # 2. congressional_cluster_signals
    # ─────────────────────────────────────────────
    if not _table_exists(cursor, 'congressional_cluster_signals'):
        cursor.execute('''
        CREATE TABLE congressional_cluster_signals (
            cluster_id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker             TEXT NOT NULL,
            buy_count          INTEGER,
            politicians_json   TEXT,         -- JSON array of politician names
            total_amount       REAL,         -- sum of estimated trade amounts
            bipartisan         BOOLEAN,      -- True if both parties buying
            committee_relevance TEXT,        -- JSON array of relevant committees
            signal_strength    REAL,         -- 0-100 score
            window_days        INTEGER,      -- rolling window used
            created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_cluster_ticker ON congressional_cluster_signals(ticker)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_cluster_created ON congressional_cluster_signals(created_at)')
        print("✓ Created congressional_cluster_signals table")
    else:
        print("  ℹ️  congressional_cluster_signals already exists")

    # ─────────────────────────────────────────────
    # 3. macro_indicators
    # ─────────────────────────────────────────────
    if not _table_exists(cursor, 'macro_indicators'):
        cursor.execute('''
        CREATE TABLE macro_indicators (
            indicator_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            yield_curve_spread REAL,         -- T10Y2Y spread in %
            fed_funds_rate     REAL,         -- FEDFUNDS %
            unemployment_rate  REAL,         -- UNRATE %
            m2_yoy_change      REAL,         -- M2 YoY % change
            hy_oas_bps         REAL,         -- HY OAS in basis points
            consumer_sentiment REAL,         -- UMCSENT index
            rate_10y           REAL,         -- DGS10 %
            rate_2y            REAL,         -- DGS2 %
            macro_score        REAL,         -- 0-100 composite score
            defcon_modifier    REAL,         -- DEFCON level adjustment
            bearish_signals    INTEGER,
            bullish_signals    INTEGER,
            signals_json       TEXT,         -- JSON array of signal objects
            created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_macro_created ON macro_indicators(created_at)')
        print("✓ Created macro_indicators table")
    else:
        print("  ℹ️  macro_indicators already exists")

        # Ensure all columns exist
        macro_columns = [
            ('yield_curve_spread', 'REAL'),
            ('fed_funds_rate', 'REAL'),
            ('unemployment_rate', 'REAL'),
            ('m2_yoy_change', 'REAL'),
            ('hy_oas_bps', 'REAL'),
            ('consumer_sentiment', 'REAL'),
            ('rate_10y', 'REAL'),
            ('rate_2y', 'REAL'),
            ('macro_score', 'REAL'),
            ('defcon_modifier', 'REAL'),
            ('bearish_signals', 'INTEGER'),
            ('bullish_signals', 'INTEGER'),
            ('signals_json', 'TEXT'),
        ]
        for col_name, col_type in macro_columns:
            if not _column_exists(cursor, 'macro_indicators', col_name):
                cursor.execute(f'ALTER TABLE macro_indicators ADD COLUMN {col_name} {col_type}')
                print(f"  ✓ Added {col_name} to macro_indicators")

    # ─────────────────────────────────────────────
    # 4. Add congressional_signal_score to news_signals table
    #    (for composite scoring integration)
    # ─────────────────────────────────────────────
    if _table_exists(cursor, 'news_signals'):
        new_news_cols = [
            ('congressional_signal_score', 'REAL DEFAULT 50'),
            ('macro_score', 'REAL DEFAULT 50'),
            ('macro_defcon_modifier', 'REAL DEFAULT 0'),
        ]
        for col_name, col_def in new_news_cols:
            if not _column_exists(cursor, 'news_signals', col_name):
                try:
                    cursor.execute(f'ALTER TABLE news_signals ADD COLUMN {col_name} {col_def}')
                    print(f"✓ Added {col_name} to news_signals")
                except sqlite3.OperationalError:
                    pass

    # ─────────────────────────────────────────────
    # 5. Add macro_defcon_modifier to signal_monitoring
    # ─────────────────────────────────────────────
    if _table_exists(cursor, 'signal_monitoring'):
        if not _column_exists(cursor, 'signal_monitoring', 'macro_defcon_modifier'):
            try:
                cursor.execute('ALTER TABLE signal_monitoring ADD COLUMN macro_defcon_modifier REAL DEFAULT 0')
                print("✓ Added macro_defcon_modifier to signal_monitoring")
            except sqlite3.OperationalError:
                pass

    conn.commit()
    conn.close()

    print(f"\n✅ Alt data schema migration complete")
    print(f"   Tables: congressional_trades, congressional_cluster_signals, macro_indicators")
    return True


if __name__ == '__main__':
    print("🔧 HighTrade Alternative Data Schema Migration\n")
    success = migrate_alt_data_schema()
    if not success:
        exit(1)
