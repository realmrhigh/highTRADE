#!/usr/bin/env python3
"""
Migration script: Extend news_signals table and create gemini_analysis table.

Safe to run multiple times - checks column existence before ALTER TABLE,
and uses CREATE TABLE IF NOT EXISTS for the new table.
"""

import sqlite3
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
DB_PATH = SCRIPT_DIR / 'trading_data' / 'trading_history.db'


# ---------------------------------------------------------------------------
# Column definitions to add to news_signals
# (name, sql_type, description)
# ---------------------------------------------------------------------------
NEWS_SIGNALS_NEW_COLUMNS = [
    (
        'sentiment_net_score',
        'REAL',
        'Weighted directional consensus (-100 to +100)',
    ),
    (
        'signal_concentration',
        'REAL',
        '0.0-1.0 - fraction of articles agreeing on dominant crisis',
    ),
    (
        'crisis_distribution_json',
        'TEXT',
        'Full breakdown e.g. {"inflation_rate": 8, "market_correction": 5}',
    ),
    (
        'score_components_json',
        'TEXT',
        'Each sub-score that contributed to the final news_score',
    ),
    (
        'keyword_hits_json',
        'TEXT',
        'Top keywords that fired across all articles with hit counts',
    ),
    (
        'baseline_deviation',
        'REAL',
        'How much this cycle deviates from the 30-day rolling average',
    ),
    (
        'articles_full_json',
        'TEXT',
        'All articles with full data including description field',
    ),
    (
        'gemini_flash_json',
        'TEXT',
        'Gemini Flash analysis output for this signal cycle',
    ),
]

# ---------------------------------------------------------------------------
# New gemini_analysis table DDL
# ---------------------------------------------------------------------------
CREATE_GEMINI_ANALYSIS_TABLE = """
CREATE TABLE IF NOT EXISTS gemini_analysis (
    analysis_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    news_signal_id       INTEGER,
    model_used           TEXT,
    trigger_type         TEXT,
    narrative_coherence  REAL,
    hidden_risks         TEXT,
    contrarian_signals   TEXT,
    market_context       TEXT,
    confidence_in_signal REAL,
    recommended_action   TEXT,
    reasoning            TEXT,
    input_tokens         INTEGER,
    output_tokens        INTEGER,
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (news_signal_id) REFERENCES news_signals (news_signal_id)
);
"""

CREATE_IDX_NEWS_SIGNAL_ID = """
CREATE INDEX IF NOT EXISTS idx_gemini_analysis_news_signal_id
    ON gemini_analysis (news_signal_id);
"""

CREATE_IDX_CREATED_AT = """
CREATE INDEX IF NOT EXISTS idx_gemini_analysis_created_at
    ON gemini_analysis (created_at DESC);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_existing_columns(cursor: sqlite3.Cursor, table: str) -> set:
    """Return the set of column names currently in *table*."""
    cursor.execute(f"PRAGMA table_info({table})")
    rows = cursor.fetchall()
    return {row[1] for row in rows}


def add_column_if_missing(
    cursor: sqlite3.Cursor,
    table: str,
    column: str,
    sql_type: str,
    description: str,
    existing_columns: set,
) -> None:
    """ALTER TABLE to add *column* only when it does not already exist."""
    if column in existing_columns:
        print(f"  [SKIP]   {table}.{column} already exists")
        return

    try:
        cursor.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"
        )
        print(f"  [ADDED]  {table}.{column} {sql_type}  -- {description}")
    except sqlite3.OperationalError as exc:
        # Catches "duplicate column name" from a concurrent process or
        # any other ALTER TABLE failure.
        print(f"  [ERROR]  {table}.{column}: {exc}")


# ---------------------------------------------------------------------------
# Main migration
# ---------------------------------------------------------------------------

def run_migration() -> None:
    print("=" * 64)
    print("HighTrade DB Migration: news_signals + gemini_analysis")
    print(f"Database : {DB_PATH}")
    print("=" * 64)

    if not DB_PATH.exists():
        print(f"\n[FATAL] Database not found at {DB_PATH}")
        raise SystemExit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")   # safe for concurrent readers
    cursor = conn.cursor()

    # ------------------------------------------------------------------
    # 1. Extend news_signals
    # ------------------------------------------------------------------
    print("\n--- Step 1: Add new columns to news_signals ---\n")

    existing = get_existing_columns(cursor, 'news_signals')
    print(f"  Current columns ({len(existing)}): {', '.join(sorted(existing))}\n")

    for col_name, col_type, col_desc in NEWS_SIGNALS_NEW_COLUMNS:
        add_column_if_missing(
            cursor, 'news_signals', col_name, col_type, col_desc, existing
        )

    # ------------------------------------------------------------------
    # 2. Create gemini_analysis table
    # ------------------------------------------------------------------
    print("\n--- Step 2: Create gemini_analysis table ---\n")

    try:
        cursor.execute(CREATE_GEMINI_ANALYSIS_TABLE)
        print("  [OK]     gemini_analysis table created (or already existed)")
    except sqlite3.OperationalError as exc:
        print(f"  [ERROR]  gemini_analysis table: {exc}")

    # ------------------------------------------------------------------
    # 3. Create indexes on gemini_analysis
    # ------------------------------------------------------------------
    print("\n--- Step 3: Create indexes on gemini_analysis ---\n")

    try:
        cursor.execute(CREATE_IDX_NEWS_SIGNAL_ID)
        print("  [OK]     idx_gemini_analysis_news_signal_id")
    except sqlite3.OperationalError as exc:
        print(f"  [ERROR]  idx_gemini_analysis_news_signal_id: {exc}")

    try:
        cursor.execute(CREATE_IDX_CREATED_AT)
        print("  [OK]     idx_gemini_analysis_created_at")
    except sqlite3.OperationalError as exc:
        print(f"  [ERROR]  idx_gemini_analysis_created_at: {exc}")

    # ------------------------------------------------------------------
    # 4. Commit and verify
    # ------------------------------------------------------------------
    conn.commit()
    conn.close()

    # Re-open for verification (proves commit landed)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    print("\n--- Verification ---\n")

    final_cols = get_existing_columns(cursor, 'news_signals')
    print(f"  news_signals columns ({len(final_cols)}):")
    for col in sorted(final_cols):
        print(f"    - {col}")

    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='gemini_analysis'"
    )
    ga_exists = cursor.fetchone() is not None
    print(f"\n  gemini_analysis table present: {ga_exists}")

    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='gemini_analysis'"
    )
    indexes = [row[0] for row in cursor.fetchall()]
    print(f"  gemini_analysis indexes: {indexes}")

    conn.close()

    print("\n" + "=" * 64)
    print("Migration complete.")
    print("=" * 64)


if __name__ == '__main__':
    run_migration()
