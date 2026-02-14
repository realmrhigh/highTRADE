#!/usr/bin/env python3
"""
HighTrade Database Setup & Schema Builder
Initializes SQLite database for crisis pattern recognition and signal monitoring
"""

import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path

# Database path
DB_PATH = Path.home() / 'trading_data' / 'trading_history.db'
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

def init_database():
    """Initialize SQLite database with complete schema"""
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()

    # Create crisis_events table - historical crisis metadata
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS crisis_events (
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
        category TEXT CHECK(category IN ('trade', 'policy', 'geopolitical', 'financial', 'epidemic')),
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    # Create signals table - detected signals for each crisis
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS signals (
        signal_id INTEGER PRIMARY KEY AUTOINCREMENT,
        crisis_id INTEGER NOT NULL,
        signal_type TEXT CHECK(signal_type IN (
            'bond_yield_spike',
            'official_denial',
            'tone_shift',
            'elite_pushback',
            'rally_despite_news',
            'retaliation_pause',
            'vix_divergence',
            'put_call_spike',
            'market_breadth_extreme',
            'policy_signal'
        )),
        signal_weight REAL NOT NULL,
        detected_date TEXT,
        detected_time TEXT,
        value TEXT,
        description TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (crisis_id) REFERENCES crisis_events(crisis_id)
    )
    ''')

    # Create market_data table - historical price/yield data
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS market_data (
        data_id INTEGER PRIMARY KEY AUTOINCREMENT,
        crisis_id INTEGER NOT NULL,
        data_date TEXT NOT NULL,
        sp500_price REAL,
        sp500_change_percent REAL,
        nasdaq_price REAL,
        nasdaq_change_percent REAL,
        vix_close REAL,
        bond_10yr_yield REAL,
        bond_yield_change_bps INTEGER,
        put_call_ratio REAL,
        market_breadth_percent REAL,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (crisis_id) REFERENCES crisis_events(crisis_id),
        UNIQUE(crisis_id, data_date)
    )
    ''')

    # Create signal_monitoring table - real-time monitoring
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS signal_monitoring (
        monitor_id INTEGER PRIMARY KEY AUTOINCREMENT,
        monitoring_date TEXT NOT NULL,
        monitoring_time TEXT NOT NULL,
        bond_10yr_yield REAL,
        bond_yield_5day_change_bps INTEGER,
        vix_close REAL,
        vix_5day_high REAL,
        news_sentiment_score REAL CHECK(news_sentiment_score >= -1 AND news_sentiment_score <= 1),
        official_denials_count INTEGER DEFAULT 0,
        defcon_level INTEGER CHECK(defcon_level IN (5, 4, 3, 2, 1)),
        signal_score REAL CHECK(signal_score >= 0 AND signal_score <= 100),
        key_articles TEXT,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(monitoring_date, monitoring_time)
    )
    ''')

    # Create trade_records table - executed trades
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS trade_records (
        trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
        crisis_id INTEGER,
        entry_date TEXT NOT NULL,
        entry_time TEXT,
        entry_price REAL NOT NULL,
        entry_signal_score REAL,
        defcon_at_entry INTEGER,
        shares INTEGER,
        position_size_dollars REAL,
        exit_date TEXT,
        exit_time TEXT,
        exit_price REAL,
        exit_reason TEXT CHECK(exit_reason IN ('profit_target', 'stop_loss', 'manual', 'invalidation')),
        profit_loss_dollars REAL,
        profit_loss_percent REAL,
        holding_hours INTEGER,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (crisis_id) REFERENCES crisis_events(crisis_id)
    )
    ''')

    # Create defcon_history table - DEFCON level changes over time
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS defcon_history (
        defcon_id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_date TEXT NOT NULL,
        event_time TEXT NOT NULL,
        defcon_level INTEGER CHECK(defcon_level IN (5, 4, 3, 2, 1)) NOT NULL,
        reason TEXT,
        contributing_signals TEXT,
        signal_score REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    # Create signal_rules table - weighting rules for signal evaluation
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS signal_rules (
        rule_id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_type TEXT UNIQUE NOT NULL,
        base_weight REAL NOT NULL,
        description TEXT,
        defcon_4_threshold REAL,
        defcon_3_threshold REAL,
        defcon_2_threshold REAL,
        defcon_1_threshold REAL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    conn.commit()
    print(f"✓ Database initialized at {DB_PATH}")

    return conn, cursor

def populate_signal_rules(cursor):
    """Populate the signal weighting rules"""
    rules = [
        ('bond_yield_spike', 0.40, 'Bond yields spike >40bps in 3 days', 20, 40, 50, 80),
        ('official_denial', 0.20, 'Multiple official denials (3+ in 48hrs)', 1, 3, 4, 5),
        ('tone_shift', 0.15, 'Leadership tone shift to reassuring', 0.5, 1, 1.5, 2),
        ('elite_pushback', 0.10, 'Bipartisan elite pushback/warnings', 1, 2, 3, 4),
        ('rally_despite_news', 0.10, 'Market rally despite bad headlines', 100, 400, 600, 1000),
        ('retaliation_pause', 0.05, 'Retaliation/escalation plateaus', 1, 2, 3, 5),
        ('vix_divergence', 0.08, 'VIX declining while prices still down', 25, 20, 15, 10),
        ('put_call_spike', 0.08, 'Put/call ratio spike (>1.5)', 1.2, 1.5, 1.8, 2.0),
        ('market_breadth_extreme', 0.12, 'Market breadth < 20% (oversold)', 25, 15, 10, 5),
        ('policy_signal', 0.18, 'Policy officials signal intervention', 1, 2, 3, 4),
    ]

    for rule in rules:
        try:
            cursor.execute('''
            INSERT OR REPLACE INTO signal_rules
            (signal_type, base_weight, description, defcon_4_threshold,
             defcon_3_threshold, defcon_2_threshold, defcon_1_threshold)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', rule)
        except Exception as e:
            print(f"  Note: {rule[0]} might already exist: {e}")

    print("✓ Signal rules configured")

def populate_historical_crises(cursor):
    """Populate 5 key historical crisis events"""
    crises = [
        {
            'name': 'April 2025 Tariff Crash ("Liberation Day")',
            'description': 'Trump announces sweeping tariffs, markets crash 15-20% in days',
            'trigger': 'Trump announces "Liberation Day" tariffs',
            'start_date': '2025-04-02',
            'crisis_bottom_date': '2025-04-08',
            'recovery_date': '2025-04-09',
            'resolution_announcement_date': '2025-04-09',
            'market_drop_percent': -15.2,
            'recovery_percent': 9.5,
            'recovery_days': 1,
            'severity': 'severe',
            'category': 'trade',
            'notes': 'Tariff pause announced April 9 midday. Largest rally since 2008/2020.'
        },
        {
            'name': 'March 2020 COVID Crash',
            'description': 'Global pandemic lockdowns, S&P drops 34% from peak',
            'trigger': 'COVID-19 pandemic declared, lockdowns announced',
            'start_date': '2020-02-19',
            'crisis_bottom_date': '2020-03-23',
            'recovery_date': '2020-08-06',
            'resolution_announcement_date': '2020-03-23',
            'market_drop_percent': -34.0,
            'recovery_percent': 67.0,
            'recovery_days': 136,
            'severity': 'severe',
            'category': 'epidemic',
            'notes': 'Fed emergency measures + vaccine development narrative drove reversal'
        },
        {
            'name': 'December 2018 Trade War Selloff',
            'description': 'Fed rate hikes + trade tensions, S&P drops 20%',
            'trigger': 'Fed rate hike + escalating China trade war rhetoric',
            'start_date': '2018-09-20',
            'crisis_bottom_date': '2018-12-24',
            'recovery_date': '2019-01-31',
            'resolution_announcement_date': '2019-01-15',
            'market_drop_percent': -20.0,
            'recovery_percent': 19.0,
            'recovery_days': 38,
            'severity': 'severe',
            'category': 'financial',
            'notes': 'Fed pivot on rate hikes (FOMC pause announced). Powell dovish shift.'
        },
        {
            'name': 'February 2018 VIX Volatility Spike',
            'description': 'VIX spike to 50, sudden volatility crush',
            'trigger': 'Fed rate hike expectations, wage data surprises',
            'start_date': '2018-02-02',
            'crisis_bottom_date': '2018-02-05',
            'recovery_date': '2018-02-07',
            'resolution_announcement_date': '2018-02-06',
            'market_drop_percent': -10.0,
            'recovery_percent': 8.0,
            'recovery_days': 5,
            'severity': 'moderate',
            'category': 'financial',
            'notes': 'Intraday reversal. Fund deleveraging purge + buyback support.'
        },
        {
            'name': 'August 2015 China Devaluation',
            'description': 'Chinese yuan devaluation shock, emerging market contagion fears',
            'trigger': 'Surprise CNY devaluation announcement',
            'start_date': '2015-08-11',
            'crisis_bottom_date': '2015-08-24',
            'recovery_date': '2015-10-02',
            'resolution_announcement_date': '2015-08-24',
            'market_drop_percent': -12.0,
            'recovery_percent': 18.0,
            'recovery_days': 39,
            'severity': 'moderate',
            'category': 'geopolitical',
            'notes': 'Fed rate hike fears eased. China stimulus signals + technical reversal.'
        },
    ]

    for crisis in crises:
        try:
            cursor.execute('''
            INSERT INTO crisis_events
            (name, description, trigger, start_date, crisis_bottom_date, recovery_date,
             resolution_announcement_date, market_drop_percent, recovery_percent,
             recovery_days, severity, category, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                crisis['name'],
                crisis['description'],
                crisis['trigger'],
                crisis['start_date'],
                crisis['crisis_bottom_date'],
                crisis['recovery_date'],
                crisis['resolution_announcement_date'],
                crisis['market_drop_percent'],
                crisis['recovery_percent'],
                crisis['recovery_days'],
                crisis['severity'],
                crisis['category'],
                crisis['notes']
            ))
        except sqlite3.IntegrityError:
            print(f"  Crisis '{crisis['name']}' already exists")

    print(f"✓ {len(crises)} historical crises populated")

def populate_sample_signals():
    """Populate signals for April 2025 crisis (detailed example)"""
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()

    # Get April 2025 crisis ID
    cursor.execute("SELECT crisis_id FROM crisis_events WHERE name = ?",
                   ("April 2025 Tariff Crash (\"Liberation Day\")",))
    result = cursor.fetchone()
    if result:
        crisis_id = result[0]
        signals = [
            (crisis_id, 'bond_yield_spike', 0.40, '2025-04-08', '14:30', '4.5%', 'Bond yields spiked >40bps in 3 days'),
            (crisis_id, 'official_denial', 0.20, '2025-04-07', '10:00', '4 denials', 'Multiple "not looking at pause" statements'),
            (crisis_id, 'tone_shift', 0.15, '2025-04-09', '07:00', 'Trump', 'BE COOL! Everything will work out'),
            (crisis_id, 'elite_pushback', 0.10, '2025-04-08', '16:00', 'Senate', 'Bipartisan Senate hearings, CEO warnings'),
            (crisis_id, 'rally_despite_news', 0.10, '2025-04-08', '14:00', '+400 Dow', 'Market rally during crisis'),
            (crisis_id, 'retaliation_pause', 0.05, '2025-04-08', '12:00', 'China 125%', 'Retaliation stopped escalating'),
        ]

        for signal in signals:
            try:
                cursor.execute('''
                INSERT INTO signals
                (crisis_id, signal_type, signal_weight, detected_date, detected_time, value, description)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', signal)
            except Exception as e:
                print(f"  Signal insertion note: {e}")

        print(f"✓ April 2025 signals populated ({len(signals)} signals)")

    conn.commit()
    conn.close()

def print_schema_summary(cursor):
    """Print summary of created schema"""
    print("\n" + "="*60)
    print("HIGHTRADE DATABASE SCHEMA SUMMARY")
    print("="*60)

    tables = {
        'crisis_events': 'Historical crisis metadata',
        'signals': 'Detected signals per crisis',
        'market_data': 'Price/yield/VIX data during crises',
        'signal_monitoring': 'Real-time monitoring',
        'trade_records': 'Executed trades',
        'defcon_history': 'DEFCON level changes',
        'signal_rules': 'Signal weighting rules'
    }

    for table, description in tables.items():
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        count = cursor.fetchone()[0]
        print(f"  {table:25} {description:35} ({count} rows)")

    print("="*60 + "\n")

if __name__ == '__main__':
    print("🚀 Initializing HighTrade Database\n")

    conn, cursor = init_database()
    populate_signal_rules(cursor)
    conn.commit()

    populate_historical_crises(cursor)
    conn.commit()

    populate_sample_signals()

    # Reconnect to get fresh cursor
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    print_schema_summary(cursor)

    print(f"✓ Database ready: {DB_PATH}")
    print("\nNext steps:")
    print("  1. Run signal monitoring: python monitoring.py")
    print("  2. Query data: python queries.py")
    print("  3. Integrate with Cowork for real-time analysis")

    conn.close()
