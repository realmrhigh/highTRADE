#!/usr/bin/env python3
"""
Test Claude Feedback Loop Implementation
Verifies all components are properly installed
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path.home() / "trading_data" / "trading_history.db"

def test_database_schema():
    """Test that claude_analysis table exists with correct schema"""
    print("🧪 Testing Database Schema...")
    
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        
        # Check claude_analysis table
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='claude_analysis'")
        if not cursor.fetchone():
            print("  ❌ claude_analysis table not found!")
            return False
        print("  ✅ claude_analysis table exists")
        
        # Check columns
        cursor.execute("PRAGMA table_info(claude_analysis)")
        columns = {col[1] for col in cursor.fetchall()}
        required = {'analysis_id', 'news_signal_id', 'enhanced_confidence', 
                   'reasoning', 'recommended_action', 'confidence_adjustment'}
        
        if not required.issubset(columns):
            print(f"  ❌ Missing columns: {required - columns}")
            return False
        print(f"  ✅ All required columns present ({len(columns)} total)")
        
        # Check defcon_history updates
        cursor.execute("PRAGMA table_info(defcon_history)")
        columns = {col[1] for col in cursor.fetchall()}
        
        if 'claude_influenced' not in columns:
            print("  ⚠️  claude_influenced column missing in defcon_history")
        else:
            print("  ✅ defcon_history audit columns present")
        
        conn.close()
        return True
        
    except Exception as e:
        print(f"  ❌ Database test failed: {e}")
        return False

def test_mcp_server():
    """Test that MCP server has new tools"""
    print("\n🧪 Testing MCP Server Tools...")
    
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("mcp_server", 
                                                       "/Users/hightrade/mcp_server.py")
        mcp_module = importlib.util.module_from_spec(spec)
        
        # Read file content
        with open("/Users/hightrade/mcp_server.py", 'r') as f:
            content = f.read()
        
        # Check for new tools
        required_tools = [
            'submit_claude_analysis',
            'get_article_details',
            '_submit_claude_analysis',
            '_get_article_details'
        ]
        
        missing = [tool for tool in required_tools if tool not in content]
        if missing:
            print(f"  ❌ Missing MCP tools: {missing}")
            return False
        
        print("  ✅ submit_claude_analysis tool found")
        print("  ✅ get_article_details tool found")
        print("  ✅ Tool implementations found")
        
        # Check for enhanced get_recent_news
        if 'articles_json' not in content:
            print("  ⚠️  get_recent_news may not include article data")
        else:
            print("  ✅ get_recent_news enhanced with article data")
        
        return True
        
    except Exception as e:
        print(f"  ❌ MCP server test failed: {e}")
        return False

def test_monitoring():
    """Test that monitoring.py integrates Claude analysis"""
    print("\n🧪 Testing Monitoring Integration...")
    
    try:
        with open("/Users/hightrade/monitoring.py", 'r') as f:
            content = f.read()
        
        checks = {
            '_check_claude_analysis': 'Claude analysis helper method',
            'claude_adjustment': 'Claude adjustment logic',
            'CLAUDE OVERRIDE': 'Claude override messaging',
            'CLAUDE CAUTION': 'Claude caution messaging'
        }
        
        all_passed = True
        for check, desc in checks.items():
            if check in content:
                print(f"  ✅ {desc} found")
            else:
                print(f"  ❌ {desc} missing")
                all_passed = False
        
        return all_passed
        
    except Exception as e:
        print(f"  ❌ Monitoring test failed: {e}")
        return False

def test_insert_sample_analysis():
    """Test inserting a sample Claude analysis"""
    print("\n🧪 Testing Sample Analysis Insertion...")
    
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        
        # Insert test analysis
        cursor.execute("""
            INSERT INTO claude_analysis
            (news_signal_id, enhanced_confidence, sentiment_override,
             risk_factors, opportunity_score, reasoning, recommended_action,
             confidence_adjustment, sources_verified, narrative_coherence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            9999,  # Test signal ID
            75.5,
            'bearish',
            '["Test risk factor 1", "Test risk factor 2"]',
            60.0,
            'This is a test analysis from the verification script',
            'WAIT',
            -10.5,
            3,
            80.0
        ))
        
        analysis_id = cursor.lastrowid
        conn.commit()
        
        # Verify insertion
        cursor.execute("SELECT * FROM claude_analysis WHERE analysis_id = ?", (analysis_id,))
        row = cursor.fetchone()
        
        if row:
            print(f"  ✅ Successfully inserted test analysis (ID: {analysis_id})")
            print(f"     Confidence: {row[2]}, Action: {row[9]}, Adjustment: {row[10]}")
            
            # Clean up
            cursor.execute("DELETE FROM claude_analysis WHERE analysis_id = ?", (analysis_id,))
            conn.commit()
            print(f"  ✅ Test data cleaned up")
        else:
            print("  ❌ Failed to verify insertion")
            return False
        
        conn.close()
        return True
        
    except Exception as e:
        print(f"  ❌ Sample insertion test failed: {e}")
        return False

def main():
    """Run all tests"""
    print("=" * 60)
    print("🚀 Claude Feedback Loop - Implementation Verification")
    print("=" * 60)
    
    if not DB_PATH.exists():
        print(f"\n❌ Database not found at {DB_PATH}")
        sys.exit(1)
    
    print(f"\n📁 Database: {DB_PATH}\n")
    
    tests = [
        test_database_schema,
        test_mcp_server,
        test_monitoring,
        test_insert_sample_analysis
    ]
    
    results = []
    for test in tests:
        results.append(test())
    
    print("\n" + "=" * 60)
    print("📊 Test Results")
    print("=" * 60)
    
    passed = sum(results)
    total = len(results)
    
    print(f"\n✅ Passed: {passed}/{total}")
    if passed == total:
        print("🎉 All tests passed! Claude Feedback Loop is ready!")
        print("\n📝 Next Steps:")
        print("   1. Restart MCP server: Ctrl+C and restart")
        print("   2. Test in Claude Desktop with get_recent_news")
        print("   3. Submit test analysis with submit_claude_analysis")
        print("   4. Monitor logs for Claude integration messages")
        return 0
    else:
        print(f"⚠️  Failed: {total - passed}/{total}")
        print("\n🔧 Please review failed tests above")
        return 1

if __name__ == "__main__":
    sys.exit(main())
