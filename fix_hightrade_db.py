#!/usr/bin/env python3
"""One-time fix: initialize hightrade.db as a valid SQLite database."""
import sqlite3
import os

db = 'trading_data/hightrade.db'
print(f'Before: {os.path.getsize(db)} bytes')

conn = sqlite3.connect(db)
conn.execute('PRAGMA journal_mode=WAL;')
conn.execute('PRAGMA page_size=4096;')
conn.commit()
conn.close()

print(f'After:  {os.path.getsize(db)} bytes')

conn2 = sqlite3.connect(db)
result = conn2.execute('PRAGMA integrity_check;').fetchone()
conn2.close()
print(f'integrity_check: {result[0]}')
