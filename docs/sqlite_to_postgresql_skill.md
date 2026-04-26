# SQLite to PostgreSQL Migration Skill Document

## Overview

This document captures the knowledge and patterns for migrating duo-moonshot's quantitative trading system from SQLite to PostgreSQL. The migration addresses scalability, performance, and data integrity requirements for production trading systems.

## Core Migration Patterns

### 1. Database Connection Management

**SQLite Pattern:**
```python
import sqlite3
conn = sqlite3.connect('/path/to/crypto_data.db')
cursor = conn.cursor()
```

**PostgreSQL Pattern:**
```python
from moonshot.db import get_postgres_db

# Option 1: Direct connection
db = get_postgres_db().connect()
cursor = db.conn.cursor()

# Option 2: Context manager
with get_postgres_db().connect() as db:
    cursor = db.conn.cursor()
    cursor.execute("SELECT ...")
```

**Key Changes:**
- Replace `sqlite3` imports with PostgreSQL connection class
- Use environment variables for connection configuration
- Implement proper connection lifecycle management
- Enable `autocommit = True` for PostgreSQL transactions

### 2. SQL Syntax Adaptation

#### Placeholders
- **SQLite**: `?` placeholder
- **PostgreSQL**: `%s` placeholder

**Example:**
```python
# SQLite
cursor.execute("SELECT * FROM table WHERE id = ? AND name = ?", (id, name))

# PostgreSQL
cursor.execute("SELECT * FROM table WHERE id = %s AND name = %s", (id, name))
```

#### Dynamic Table Names
- **SQLite**: No quoting needed
- **PostgreSQL**: Must use double quotes for case-sensitive names

**Example:**
```python
# SQLite (table names case-insensitive)
table_name = f'Kline5m_{symbol}'
cursor.execute(f"SELECT * FROM {table_name} WHERE ...")

# PostgreSQL (table names case-sensitive)
table_name = f'K5m{symbol}'
cursor.execute(f'SELECT * FROM "{table_name}" WHERE ...')
```

#### System Table Queries
- **SQLite**: `sqlite_master` table
- **PostgreSQL**: `information_schema.tables` view

**Example:**
```python
# SQLite table existence check
cursor.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
    (table_name,)
)

# PostgreSQL table existence check
cursor.execute(
    "SELECT table_name FROM information_schema.tables WHERE table_name = %s",
    (table_name,)
)
```

### 3. Table Structure Changes

#### K-line Table Naming Convention
- **SQLite**: `Kline5m_{symbol}`, `Kline1h_{symbol}`, `Kline1d_{symbol}`
- **PostgreSQL**: `K5m{symbol}`, `K1h{symbol}`, `K1d{symbol}`

**Migration Logic:**
```python
def convert_table_name(sqlite_table: str) -> str:
    """Convert SQLite table name to PostgreSQL format."""
    if sqlite_table.startswith('Kline5m_'):
        return f"K5m{sqlite_table[8:]}"
    elif sqlite_table.startswith('Kline1h_'):
        return f"K1h{sqlite_table[8:]}"
    elif sqlite_table.startswith('Kline1d_'):
        return f"K1d{sqlite_table[8:]}"
    return sqlite_table
```

#### Funding Rate Table Restructuring
**Critical Change:**
- **SQLite**: Single table `funding_rate_history` with `symbol` column
- **PostgreSQL**: Per-symbol tables `FR{symbol}` (e.g., `FRSCRTUSDT`)

**SQLite Query:**
```python
cursor.execute('''
    SELECT funding_time, funding_rate
    FROM funding_rate_history
    WHERE symbol = ?
      AND funding_time >= ?
      AND funding_time < ?
    ORDER BY funding_time
''', (symbol, entry_time, exit_time))
```

**PostgreSQL Query:**
```python
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

### 4. Temporal Table Replacement

#### SQLite Temporary Tables → In-Memory Dictionaries
**Original SQLite Approach:**
```python
# Create temporary table for pending signals
self._raw_conn.execute('''
    CREATE TEMPORARY TABLE IF NOT EXISTS PendingSignals (
        symbol TEXT PRIMARY KEY,
        signal_hour TEXT,
        surge_ratio REAL,
        created_at TEXT
    )
''')

# Add signal
cursor.execute('''
    INSERT OR REPLACE INTO PendingSignals (symbol, signal_hour, surge_ratio, created_at)
    VALUES (?, ?, ?, ?)
''', (symbol, signal_hour, surge_ratio, created_at))
```

**PostgreSQL In-Memory Approach:**
```python
# In-memory dictionary
self._pending_signals = {}

def _add_pending_signal(self, symbol: str, signal_hour: str, surge_ratio: float):
    """Add a pending signal to memory."""
    self._pending_signals[symbol] = {
        'signal_hour': signal_hour,
        'surge_ratio': surge_ratio,
        'created_at': datetime.now(timezone.utc).isoformat()
    }

def _get_pending_signals(self):
    """Get all pending signals."""
    return self._pending_signals
```

## Common Issues and Solutions

### Issue 1: Table Not Found (Case Sensitivity)
**Error:** `relation "k5mbtcusdt" does not exist`
**Cause:** PostgreSQL converts unquoted identifiers to lowercase
**Solution:** Always use double quotes for dynamic table names
```python
# ❌ Wrong
cursor.execute(f'SELECT * FROM {table_name} WHERE ...')

# ✅ Correct
cursor.execute(f'SELECT * FROM "{table_name}" WHERE ...')
```

### Issue 2: Funding Rate Query Failure
**Error:** `relation "funding_rate_history" does not exist`
**Cause:** Table structure changed from unified to per-symbol
**Solution:** Use dynamic table names and timestamp comparison
```python
# Old (SQLite)
cursor.execute('SELECT ... FROM funding_rate_history WHERE symbol = %s ...')

# New (PostgreSQL)
table_name = f'FR{symbol}'
cursor.execute(f'SELECT ... FROM "{table_name}" WHERE ...')
```

### Issue 3: Missing datetime_to_timestamp Method
**Error:** `AttributeError: 'BuySurgeBacktest' object has no attribute 'datetime_to_timestamp'`
**Solution:** Add timestamp conversion utility method
```python
@staticmethod
def datetime_to_timestamp(dt: datetime) -> int:
    """Convert datetime object to milliseconds timestamp."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)
```

### Issue 4: Connection Pooling Exhaustion
**Symptoms:** `psycopg2.OperationalError: too many connections`
**Solution:** Implement connection pooling or context managers
```python
def get_db_connection():
    """Get a database connection with pooling."""
    # Use connection pool or ensure connections are closed
    db = get_postgres_db().connect()
    return db.conn

# Always use context managers
with get_postgres_db().connect() as db:
    cursor = db.conn.cursor()
    # ... operations ...
# Connection automatically closed
```

## Migration Tools

### 1. Data Migration Script (`migrate_sqlite_to_postgresql.py`)
```bash
# Check SQLite databases
python scripts/migrate_sqlite_to_postgresql.py check-sqlite

# Check PostgreSQL connection
python scripts/migrate_sqlite_to_postgresql.py check-postgresql

# Dry run migration
python scripts/migrate_sqlite_to_postgresql.py migrate --dry-run

# Perform migration
python scripts/migrate_sqlite_to_postgresql.py migrate --all

# Verify migration
python scripts/migrate_sqlite_to_postgresql.py verify
```

**Key Features:**
- Inspects SQLite database structure
- Creates PostgreSQL tables with proper schema
- Migrates K-line data with table name conversion
- Handles funding rate data restructuring
- Provides progress reporting and error handling

### 2. Code Conversion Tool (`convert_sqlite_to_postgresql.py`)
```bash
# Scan for SQLite patterns
python scripts/convert_sqlite_to_postgresql.py scan /path/to/code

# Convert code (dry run)
python scripts/convert_sqlite_to_postgresql.py convert /path/to/code --dry-run

# Convert code (actual)
python scripts/convert_sqlite_to_postgresql.py convert /path/to/code
```

**Patterns Detected and Converted:**
- SQL placeholder conversion (`?` → `%s`)
- Dynamic table name quoting
- System table query updates
- Funding rate table structure changes
- Database connection code updates
- Import statement replacements

### 3. Environment Configuration Template
```env
# PostgreSQL Configuration
PG_HOST=192.168.2.250
PG_PORT=5432
PG_DB=crypto_data
PG_USER=postgres
PG_PASSWORD=your_password_here

# SQLite Database Paths (for migration)
SQLITE_CRYPTO_PATH=/Users/liuyingnan/Documents/binance/top2/db/crypto_data.db
SQLITE_TRADER_PATH=/Users/liuyingnan/Documents/binance/top2/db/top_trader_data.db

# Application Settings
LOG_LEVEL=INFO
```

## Step-by-Step Migration Guide

### Phase 1: Preparation
1. **Environment Setup**
   ```bash
   pip install psycopg2-binary python-dotenv
   cp .env.example .env
   # Edit .env with PostgreSQL credentials
   ```

2. **Database Inspection**
   ```bash
   python scripts/migrate_sqlite_to_postgresql.py check-sqlite
   python scripts/migrate_sqlite_to_postgresql.py check-postgresql
   ```

3. **Code Analysis**
   ```bash
   python scripts/convert_sqlite_to_postgresql.py scan moonshot/
   ```

### Phase 2: Data Migration
1. **Test Migration**
   ```bash
   python scripts/migrate_sqlite_to_postgresql.py migrate --dry-run --tables-limit 5
   ```

2. **Full Migration**
   ```bash
   python scripts/migrate_sqlite_to_postgresql.py migrate --all
   ```

3. **Verification**
   ```bash
   python scripts/migrate_sqlite_to_postgresql.py verify
   ```

### Phase 3: Code Migration
1. **Backup Codebase**
   ```bash
   cp -r moonshot/ moonshot_backup_$(date +%Y%m%d)/
   ```

2. **Convert Code**
   ```bash
   python scripts/convert_sqlite_to_postgresql.py convert moonshot/ --backup
   ```

3. **Test Syntax**
   ```bash
   python -m py_compile moonshot/*.py
   ```

### Phase 4: Testing
1. **Unit Tests**
   ```python
   # Test database connection
   db = get_postgres_db().connect()
   assert db.check_connection() == True
   
   # Test table queries
   cursor = db.conn.cursor()
   cursor.execute('SELECT COUNT(*) FROM "K5mBTCUSDT" LIMIT 1')
   count = cursor.fetchone()[0]
   assert count > 0
   ```

2. **Integration Tests**
   - Run backtest with small dataset
   - Verify funding rate calculations
   - Check trade report generation

3. **Performance Tests**
   - Compare query execution times
   - Monitor memory usage
   - Test concurrent connections

## Code Examples

### Complete Migration Example: hm1l20260418.py

**Before (SQLite):**
```python
import sqlite3
from datetime import datetime

class BuySurgeBacktest:
    def __init__(self):
        self.conn = sqlite3.connect('crypto_data.db')
        self._setup_pending_signals_table()
    
    def _setup_pending_signals_table(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TEMPORARY TABLE IF NOT EXISTS PendingSignals (
                symbol TEXT PRIMARY KEY,
                signal_hour TEXT,
                surge_ratio REAL,
                created_at TEXT
            )
        ''')
    
    def calculate_funding_fee_cost(self, symbol: str, entry_datetime: datetime, exit_datetime: datetime):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT funding_time, funding_rate
            FROM funding_rate_history
            WHERE symbol = ?
              AND funding_time >= ?
              AND funding_time < ?
            ORDER BY funding_time
        ''', (symbol, entry_datetime.strftime('%Y-%m-%d %H:%M:%S'), exit_datetime.strftime('%Y-%m-%d %H:%M:%S')))
        return cursor.fetchall()
    
    def get_5m_data(self, symbol: str, target_time: datetime):
        table_name = f'Kline5m_{symbol}'
        target_ms = int(target_time.timestamp() * 1000)
        cursor = self.conn.cursor()
        cursor.execute(f'''
            SELECT close
            FROM {table_name}
            WHERE open_time = ?
            LIMIT 1
        ''', (target_ms,))
        return cursor.fetchone()
```

**After (PostgreSQL):**
```python
from moonshot.db import get_postgres_db
from datetime import datetime, timezone

class BuySurgeBacktest:
    def __init__(self):
        self._raw_conn = get_postgres_db().connect().conn
        self._pending_signals = {}  # In-memory replacement
    
    @staticmethod
    def datetime_to_timestamp(dt: datetime) -> int:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    
    def calculate_funding_fee_cost(self, symbol: str, entry_datetime: datetime, exit_datetime: datetime):
        cursor = self._raw_conn.cursor()
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
        return cursor.fetchall()
    
    def get_5m_data(self, symbol: str, target_time: datetime):
        table_name = f'K5m{symbol}'
        target_ms = self.datetime_to_timestamp(target_time)
        cursor = self._raw_conn.cursor()
        cursor.execute(f'''
            SELECT close
            FROM "{table_name}"
            WHERE open_time = %s
            LIMIT 1
        ''', (target_ms,))
        return cursor.fetchone()
```

## Best Practices

### 1. Connection Management
- Always use context managers for database connections
- Implement connection pooling for high-concurrency scenarios
- Set appropriate timeout values
- Enable autocommit for read-heavy operations

### 2. Error Handling
```python
try:
    cursor.execute('SELECT ... FROM "{table_name}" WHERE ...', params)
    result = cursor.fetchall()
except psycopg2.Error as e:
    if 'does not exist' in str(e):
        logger.warning(f"Table {table_name} does not exist")
        return []
    elif 'connection' in str(e).lower():
        logger.error(f"Database connection error: {e}")
        raise
    else:
        logger.error(f"Database error: {e}")
        raise
```

### 3. Performance Optimization
- Use batch inserts for data migration
- Create indexes on frequently queried columns
- Use `EXPLAIN ANALYZE` to optimize slow queries
- Implement query timeouts for long-running operations

### 4. Testing Strategy
- Create migration test suite
- Verify data consistency between SQLite and PostgreSQL
- Test edge cases (missing tables, empty results)
- Performance benchmark comparison

## Troubleshooting Checklist

### Pre-Migration
- [ ] PostgreSQL server is running and accessible
- [ ] Database user has appropriate permissions
- [ ] `.env` file is properly configured
- [ ] Required Python packages are installed

### During Migration
- [ ] SQLite database files are readable
- [ ] Table name conversions are correct
- [ ] Data types are properly mapped
- [ ] Funding rate data is restructured correctly

### Post-Migration
- [ ] All tables exist in PostgreSQL
- [ ] Row counts match between databases
- [ ] Code compiles without syntax errors
- [ ] Backtests run successfully
- [ ] Performance meets expectations

## Remaining SQLite Dependencies

### Files Still Using SQLite:
1. **`jchc.py`** - Backtest verification tool
   - **Status**: Needs PostgreSQL adaptation
   - **Action**: Convert similar to main codebase

2. **`paper/paper_store.py`** - Paper trading storage
   - **Status**: Local persistence
   - **Action**: Consider keeping SQLite or migrating for consistency

### Migration Priority:
1. **High**: `jchc.py` (used for backtest validation)
2. **Medium**: Paper trading storage (local use only)

## Monitoring and Maintenance

### Key Metrics to Monitor:
- Database connection count
- Query execution time
- Memory usage
- Disk I/O for PostgreSQL data directory

### Alerting Conditions:
- Connection pool exhaustion
- Query timeouts (> 5 seconds)
- Database disk space > 80%
- Failed connection attempts

### Maintenance Tasks:
- Regular vacuum and analyze operations
- Index rebuilding for fragmented tables
- Backup verification
- Connection pool health checks

## Conclusion

The SQLite to PostgreSQL migration significantly enhances duo-moonshot's capabilities for production trading environments. By following the patterns and tools outlined in this document, teams can ensure a smooth transition with minimal disruption to trading operations.

Key benefits of PostgreSQL migration:
1. **Scalability**: Handle larger datasets and concurrent connections
2. **Performance**: Advanced query optimization and indexing
3. **Reliability**: ACID compliance and transaction support
4. **Maintainability**: Standardized SQL syntax and tooling
5. **Ecosystem**: Integration with monitoring and backup solutions

Remember to test thoroughly in a staging environment before deploying to production, and maintain comprehensive logging throughout the migration process.