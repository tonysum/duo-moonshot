"""Verify CSV results against database.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from moonshot.db import get_postgres_db as get_db
from moonshot.data_feed import DataFeed

def verify(csv_path: str):
    path = Path(csv_path)
    if not path.exists():
        print(f"❌ File not found: {path}")
        return

    print(f"📋 Verifying {path.name}...")
    db = get_db()
    db.connect()
    feed = DataFeed(db)
    
    errors = 0
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(row for row in f if not row.startswith("#"))
        for row in reader:
            symbol = row.get("Symbol") or row.get("交易对")
            if not symbol: continue
            
            entry_time_str = row.get("Date") or row.get("信号日期")
            if not entry_time_str: continue
            
            # Simple check: verify symbol exists in DB
            try:
                # We'll just check if we can load some info
                feed.load_listing_date(symbol)
            except Exception as e:
                print(f"❌ Error for {symbol}: {e}")
                errors += 1
                
    db.close()
    if errors == 0:
        print("🎉 Verification passed (basic symbols check).")
    else:
        print(f"💥 Found {errors} errors.")

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        verify(sys.argv[1])
    else:
        print("Usage: python -m moonshot.verify <csv_path>")
