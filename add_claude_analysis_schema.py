#!/usr/bin/env python3
"""
Add Claude Analysis Feedback Schema
Creates table to store Claude's enhanced news analysis for AI-augmented trading
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path.home() / "trading_data" / "trading_history.db"

def add_claude_analysis_table():
    """Create claude_analysis table for storing Claude's enhanced analysis"""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        
        print("🧠 Creating claude_analysis table...")
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS claude_analysis (
                analysis_id INTEGER PRIMARY KEY AUTOINCREMENT,
                news_signal_id INTEGER,
                enhanced_confidence REAL,
                sentiment_override TEXT,
                risk_factors TEXT,
                opportunity_score REAL,
                reasoning TEXT,
                sources_verified INTEGER,
                narrative_coherence REAL,
                recommended_action TEXT,
                confidence_adjustment REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (news_signal_id) REFERENCES news_signals(news_signal_id)
            )
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_claude_news_signal 
            ON claude_analysis(news_signal_id)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_claude_timestamp 
            ON claude_analysis(created_at DESC)
        """)
        
        conn.commit()
        print("✅ claude_analysis table created successfully!")
        
        cursor.execute("PRAGMA table_info(claude_analysis)")
        columns = cursor.fetchall()
        print("\n📊 Table structure:")
        for col in columns:
            print(f"   {col[1]}: {col[2]}")
        
        conn.close()
        return True
        
    except Exception as e:
        print(f"❌ Error creating table: {e}")
        return False

def update_defcon_history_table():
    """Add Claude tracking columns to defcon_history table"""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        
        print("\n🔧 Updating defcon_history table for audit trail...")
        
        cursor.execute("PRAGMA table_info(defcon_history)")
        columns = [col[1] for col in cursor.fetchall()]
        
        if 'claude_influenced' not in columns:
            cursor.execute("ALTER TABLE defcon_history ADD COLUMN claude_influenced BOOLEAN DEFAULT 0")
            print("   ✅ Added claude_influenced column")
        else:
            print("   ℹ️  claude_influenced column already exists")
        
        if 'claude_analysis_id' not in columns:
            cursor.execute("ALTER TABLE defcon_history ADD COLUMN claude_analysis_id INTEGER")
            print("   ✅ Added claude_analysis_id column")
        else:
            print("   ℹ️  claude_analysis_id column already exists")
        
        conn.commit()
        conn.close()
        print("✅ defcon_history table updated successfully!")
        return True
        
    except Exception as e:
        print(f"❌ Error updating defcon_history: {e}")
        return False

def main():
    """Run schema migration"""
    print("=" * 60)
    print("🚀 Claude Analysis Feedback Loop - Schema Migration")
    print("=" * 60)
    
    if not DB_PATH.exists():
        print(f"❌ Database not found at {DB_PATH}")
        print("   Please run setup_database.py first")
        sys.exit(1)
    
    print(f"📁 Database: {DB_PATH}\n")
    
    success1 = add_claude_analysis_table()
    success2 = update_defcon_history_table()
    
    if success1 and success2:
        print("\n" + "=" * 60)
        print("🎉 Schema migration completed successfully!")
        print("=" * 60)
        print("\n📝 Next steps:")
        print("   1. Update mcp_server.py to add submit_claude_analysis tool")
        print("   2. Update monitoring.py to integrate Claude analysis")
        print("   3. Test the feedback loop with Claude Desktop")
    else:
        print("\n❌ Schema migration failed")
        sys.exit(1)

if __name__ == "__main__":
    main()
