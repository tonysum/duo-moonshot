# SQLite to PostgreSQL Migration Guide

## Overview

This document outlines the migration of duo-moonshot's database layer from SQLite to PostgreSQL. The migration was necessary to handle larger datasets, improve concurrency, and support more complex queries in the quantitative trading system.

## Database Architecture Differences

### SQLite Architecture
- **Database Files**: Multiple `.db` files (`crypto_data.db`, `top_trader_data.db`)
- **Table Naming**: Dynamic table names (e.g., `Kline5m_{symbol}`)
- **Connection Model**: File-based, single connection per file
- **Schema Storage**: `sqlite_master` system table

### PostgreSQL Architecture  
- **Single Database**: All tables in one PostgreSQL database
- **Table Naming Conventions**:
  - 5-minute K-line: `K5m{symbol}` (e.g., `K5mBTCUSDT`)
  - Hourly K-line: `K1h{symbol}` (e.g., `K1hBTCUSDT`)
  - Daily K-line: `K1d{symbol}` (e.g., `K1dBTCUSDT`)
  - Funding Rate: `FR{symbol}` (e.g., `FRSCRTUSDT`)
- **Connection Model**: Client-server, connection pooling
- **Schema Storage**: `information_schema.tables`

## Key Technical Changes

### 1. Database Connection Management

**Before (SQLite)**:
```python
import sqlite3
conn = sqlite3.connect('/path/to/crypto_data.db')
```

**After (PostgreSQL)**:
```python
from moonshot.db import get_postgres_db

class PostgresDatabase:
    def __init__(self) -> None:
        self.host = os.getenv("PG_HOST", "localhost")
        self.port = int(os.getenv("PG_PORT", "5432"))
        self.database = os.getenv("PG_DB", "crypto_data")
        self.user = os.getenv("PG_USER", "postgres")
        self.password = os.getenv("PG_PASSWORD", "")
        
    def connect(self) -> PostgresDatabase:
        import psycopg2
        self._conn = psycopg2.connect(
            host=self.host,
            port=self.port,
            database=self.database,
            user=self.user,
            password=self.password,
        )
        self._conn.autocommit = True
        return self
```

### 2. SQL Syntax Adaptation

| Aspect | SQLite | PostgreSQL |
|--------|--------|------------|
| Placeholders | `?` | `%s` |
| Table name quoting | Not required | Double quotes for case-sensitive names |
| System tables | `sqlite_master` | `information_schema.tables` |
| Table existence check | Check file existence | Query `information_schema.tables` |

**Example: Table existence check**:

```python
# SQLite
cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))

# PostgreSQL  
cursor.execute(
    "SELECT table_name FROM information_schema.tables WHERE table_name = %s",
    (table_name,)
)
```

### 3. Dynamic Table Names

Always use double quotes around dynamic table names in PostgreSQL:

```python
# ❌ Wrong (PostgreSQL converts to lowercase)
table_name = f'K5m{symbol}'
cursor.execute(f'SELECT close FROM {table_name} WHERE open_time = %s', (target_ms,))

# ✅ Correct (Preserves case)
table_name = f'K5m{symbol}'
cursor.execute(f'SELECT close FROM "{table_name}" WHERE open_time = %s', (target_ms,))
```

### 4. Funding Rate Table Structure Change

**Critical Change**: Funding rate tables changed from a unified table to per-symbol tables:

| Before | After |
|--------|-------|
| Single table: `funding_rate_history` | Per-symbol tables: `FR{symbol}` |
| Contains `symbol` column | No `symbol` column (implied by table name) |
| `funding_time` as string | `funding_time` as bigint (milliseconds) |

**Query Update**:
```python
# Before: Unified table with symbol filter
cursor.execute('''
    SELECT funding_time, funding_rate
    FROM funding_rate_history
    WHERE symbol = %s
      AND funding_time >= %s
      AND funding_time < %s
    ORDER BY funding_time
''', (symbol, entry_datetime.strftime('%Y-%m-%d %H:%M:%S'), exit_datetime.strftime('%Y-%m-%d %H:%M:%S')))

# After: Dynamic table name with timestamp comparison
table_name = f'FR{symbol}'
entry_ms = self.datetime_to_timestamp(entry_datetime)
exit_ms = self.datetime_to_timestamp(exit_datetime)

cursor.execute(f'''
    SELECT funding_time, funding_rate
    FROM "{table_name}"
    WHERE funding_time >= %s
      AND funding_time < %s
    ORDER BY funding_time
''', (entry_ms, exit_ms))
```

### 5. Temporary Table Replacement

SQLite temporary tables were replaced with in-memory dictionaries for better performance:

```python
# Before: SQLite temporary table for pending signals
self._raw_conn.execute('''
    CREATE TEMPORARY TABLE IF NOT EXISTS PendingSignals (
        symbol TEXT PRIMARY KEY,
        signal_hour TEXT,
        surge_ratio REAL,
        created_at TEXT
    )
''')

# After: In-memory dictionary
self._pending_signals = {}  # symbol -> {signal_hour, surge_ratio, created_at}
```

## Migration Steps

### Step 1: Environment Setup
1. Install PostgreSQL dependencies:
   ```bash
   pip install psycopg2-binary python-dotenv
   ```

2. Configure environment variables (`.env` file):
   ```env
   PG_HOST=192.168.2.250
   PG_PORT=5432
   PG_DB=crypto_data
   PG_USER=postgres
   PG_PASSWORD=your_password
   ```

### Step 2: Database Connection Refactoring
1. Replace SQLite imports with PostgreSQL connection class
2. Update all connection creation calls
3. Ensure proper connection lifecycle management

### Step 3: SQL Query Updates
1. Change `?` placeholders to `%s`
2. Add double quotes around dynamic table names
3. Update system table queries
4. Adapt funding rate queries to new table structure

### Step 4: Memory Optimization
1. Replace temporary tables with in-memory data structures
2. Implement connection pooling where needed
3. Add proper error handling for missing tables

## Common Issues and Solutions

### Issue 1: Table Not Found (Case Sensitivity)
**Error**: `relation "k5mbtcusdt" does not exist`

**Cause**: PostgreSQL converts unquoted identifiers to lowercase

**Solution**: Always use double quotes for dynamic table names:
```python
cursor.execute(f'SELECT * FROM "{table_name}" WHERE ...')
```

### Issue 2: Funding Rate Query Failure
**Error**: `relation "funding_rate_history" does not exist`

**Cause**: Table structure changed to per-symbol `FR{symbol}` format

**Solution**: Use dynamic table names and timestamp comparison:
```python
table_name = f'FR{symbol}'
entry_ms = self.datetime_to_timestamp(entry_datetime)
exit_ms = self.datetime_to_timestamp(exit_datetime)
```

### Issue 3: PendingSignals Table Missing
**Error**: `relation "pendingsignals" does not exist`

**Cause**: Temporary tables removed in favor of memory storage

**Solution**: Replace with in-memory dictionary implementation

### Issue 4: Connection Pooling Issues
**Symptoms**: Connection timeouts or "too many connections" errors

**Solution**: Implement connection pooling or use connection context managers:
```python
def get_postgres_db() -> PostgresDatabase:
    """Get a configured PostgreSQL database instance."""
    return PostgresDatabase()

# Usage with context manager
with get_postgres_db().connect() as db:
    cursor = db.conn.cursor()
    cursor.execute("SELECT ...")
```

## Validation Checklist

### ✅ Database Connectivity
- [ ] PostgreSQL server reachable
- [ ] Credentials correct
- [ ] Database exists and accessible

### ✅ Table Structure
- [ ] K-line tables exist (`K5m*`, `K1h*`, `K1d*`)
- [ ] Funding rate tables exist (`FR*`)
- [ ] Table names follow uppercase convention

### ✅ Query Compatibility
- [ ] All placeholders updated (`?` → `%s`)
- [ ] Dynamic table names properly quoted
- [ ] Timestamp formats consistent
- [ ] System table queries updated

### ✅ Performance
- [ ] Connection pooling implemented
- [ ] Memory usage optimized
- [ ] Query performance acceptable

### ✅ Error Handling
- [ ] Graceful degradation for missing tables
- [ ] Connection error recovery
- [ ] Comprehensive logging

## Code Examples

### Complete Migration Example

**Before (SQLite)**:
```python
import sqlite3
from datetime import datetime

class BuySurgeBacktest:
    def __init__(self):
        self.conn = sqlite3.connect('crypto_data.db')
    
    def get_5m_data(self, symbol: str, target_time: datetime):
        cursor = self.conn.cursor()
        table_name = f'Kline5m_{symbol}'
        target_ms = int(target_time.timestamp() * 1000)
        
        cursor.execute(f'''
            SELECT close
            FROM {table_name}
            WHERE open_time = ?
            LIMIT 1
        ''', (target_ms,))
        
        return cursor.fetchone()
```

**After (PostgreSQL)**:
```python
from moonshot.db import get_postgres_db
from datetime import datetime, timezone

class BuySurgeBacktest:
    def __init__(self):
        self._raw_conn = get_postgres_db().connect().conn
    
    @staticmethod
    def datetime_to_timestamp(dt: datetime) -> int:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    
    def get_5m_data(self, symbol: str, target_time: datetime):
        cursor = self._raw_conn.cursor()
        table_name = f'K5m{symbol}'
        target_ms = self.datetime_to_timestamp(target_time)
        
        cursor.execute(f'''
            SELECT close
            FROM "{table_name}"
            WHERE open_time = %s
            LIMIT 1
        ''', (target_ms,))
        
        return cursor.fetchone()
```

## Best Practices

1. **Connection Management**: Always use context managers or ensure connections are properly closed
2. **Table Naming**: Maintain consistent uppercase convention for dynamic table names
3. **Error Handling**: Implement graceful fallbacks for missing tables or data
4. **Logging**: Add detailed logging for database operations and migration steps
5. **Testing**: Validate migration with a subset of data before full deployment
6. **Backward Compatibility**: Keep SQLite version available for rollback if needed

## Remaining SQLite Dependencies

Some components may still use SQLite and need future migration:

### `jchc.py` - Backtest Checker
- Uses SQLite for verification
- Needs PostgreSQL adaptation similar to main codebase

### `paper/paper_store.py` - Paper Trading Storage
- Uses SQLite for local persistence
- Consider keeping SQLite for local storage or migrating to PostgreSQL for consistency

## Troubleshooting

### Log Analysis
Check for these common warning/error patterns:

1. **"relation does not exist"**: Missing double quotes or wrong table name
2. **"column does not exist"**: Schema mismatch between SQLite and PostgreSQL
3. **"syntax error at or near"**: SQL syntax differences
4. **"connection refused"**: Network or authentication issues

### Diagnostic Queries
Use these queries to verify PostgreSQL setup:

```sql
-- Check table existence
SELECT table_name FROM information_schema.tables 
WHERE table_schema='public' AND table_name LIKE 'K5m%' LIMIT 5;

-- Check funding rate tables
SELECT table_name FROM information_schema.tables 
WHERE table_schema='public' AND table_name LIKE 'FR%' LIMIT 5;

-- Test data access
SELECT COUNT(*) FROM "K5mBTCUSDT" LIMIT 1;
```

## Conclusion

The migration from SQLite to PostgreSQL provides significant benefits for duo-moonshot's quantitative trading system, including better performance, scalability, and data integrity. By following this guide and addressing the key technical challenges outlined, you can ensure a smooth transition with minimal disruption to trading operations.

Remember to test thoroughly in a staging environment before deploying to production, and maintain comprehensive logging to quickly identify and resolve any migration-related issues.