#!/usr/bin/env python3
"""
SQLite to PostgreSQL Migration Helper for duo-moonshot

This script helps migrate data from SQLite databases to PostgreSQL
for the duo-moonshot quantitative trading system.

Usage:
    python migrate_sqlite_to_postgresql.py --help
    python migrate_sqlite_to_postgresql.py check-sqlite
    python migrate_sqlite_to_postgresql.py migrate --dry-run
    python migrate_sqlite_to_postgresql.py migrate --all
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

try:
    import psycopg2
    from psycopg2.extras import DictCursor
    from dotenv import load_dotenv
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Please install required packages:")
    print("  pip install psycopg2-binary python-dotenv")
    sys.exit(1)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Default SQLite database paths (adjust as needed)
DEFAULT_SQLITE_PATHS = {
    'crypto_data': '/Users/liuyingnan/Documents/binance/top2/db/crypto_data.db',
    'top_trader_data': '/Users/liuyingnan/Documents/binance/top2/db/top_trader_data.db'
}

# Table type mappings
TABLE_TYPE_MAPPINGS = {
    'Kline5m_': 'K5m',    # SQLite: Kline5m_BTCUSDT -> PostgreSQL: K5mBTCUSDT
    'Kline1h_': 'K1h',    # SQLite: Kline1h_BTCUSDT -> PostgreSQL: K1hBTCUSDT
    'Kline1d_': 'K1d',    # SQLite: Kline1d_BTCUSDT -> PostgreSQL: K1dBTCUSDT
    # funding_rate_history handled specially
}

class SQLiteInspector:
    """Inspect SQLite database structure and data."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = None

    def __enter__(self):
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            self.conn.close()

    def get_table_names(self) -> List[str]:
        """Get all table names in the database."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        return [row[0] for row in cursor.fetchall()]

    def get_table_info(self, table_name: str) -> List[Dict[str, Any]]:
        """Get column information for a table."""
        cursor = self.conn.cursor()
        cursor.execute(f"PRAGMA table_info({table_name})")
        columns = []
        for row in cursor.fetchall():
            columns.append({
                'cid': row[0],
                'name': row[1],
                'type': row[2],
                'notnull': row[3],
                'default': row[4],
                'pk': row[5]
            })
        return columns

    def get_table_row_count(self, table_name: str) -> int:
        """Get row count for a table."""
        cursor = self.conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
        return cursor.fetchone()[0]

    def get_table_sample(self, table_name: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Get sample rows from a table."""
        cursor = self.conn.cursor()
        cursor.execute(f"SELECT * FROM {table_name} LIMIT ?", (limit,))
        rows = cursor.fetchall()

        # Get column names
        cursor.execute(f"PRAGMA table_info({table_name})")
        columns = [row[1] for row in cursor.fetchall()]

        return [dict(zip(columns, row)) for row in rows]

    def get_all_table_stats(self) -> Dict[str, Dict[str, Any]]:
        """Get statistics for all tables."""
        stats = {}
        for table_name in self.get_table_names():
            try:
                row_count = self.get_table_row_count(table_name)
                columns = self.get_table_info(table_name)
                stats[table_name] = {
                    'row_count': row_count,
                    'column_count': len(columns),
                    'columns': [col['name'] for col in columns],
                    'types': {col['name']: col['type'] for col in columns}
                }
            except Exception as e:
                logger.warning(f"Error getting stats for table {table_name}: {e}")
                stats[table_name] = {'error': str(e)}

        return stats

    def categorize_tables(self) -> Dict[str, List[str]]:
        """Categorize tables by type."""
        tables = self.get_table_names()
        categorized = {
            'kline_5m': [],
            'kline_1h': [],
            'kline_1d': [],
            'funding_rate': [],
            'other': []
        }

        for table in tables:
            if table.startswith('Kline5m_'):
                categorized['kline_5m'].append(table)
            elif table.startswith('Kline1h_'):
                categorized['kline_1h'].append(table)
            elif table.startswith('Kline1d_'):
                categorized['kline_1d'].append(table)
            elif table == 'funding_rate_history':
                categorized['funding_rate'].append(table)
            elif table in ['top_account_ratio', 'top_trader_long_short_account_ratio']:
                categorized['other'].append(table)
            else:
                categorized['other'].append(table)

        return categorized

    def extract_symbol_from_table(self, table_name: str) -> Optional[str]:
        """Extract symbol from table name based on pattern."""
        for prefix, new_prefix in TABLE_TYPE_MAPPINGS.items():
            if table_name.startswith(prefix):
                return table_name[len(prefix):]

        # Check for other patterns
        if table_name.startswith('Kline'):
            # Try to extract symbol from patterns like Kline5m_BTCUSDT
            parts = table_name.split('_')
            if len(parts) >= 2:
                return parts[1]

        return None


class PostgreSQLConnector:
    """Connect to and interact with PostgreSQL database."""

    def __init__(self, host: str, port: int, database: str,
                 user: str, password: str):
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password
        self.conn = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()

    def connect(self):
        """Establish PostgreSQL connection."""
        try:
            self.conn = psycopg2.connect(
                host=self.host,
                port=self.port,
                database=self.database,
                user=self.user,
                password=self.password
            )
            self.conn.autocommit = True
            logger.info(f"Connected to PostgreSQL at {self.host}:{self.port}/{self.database}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to PostgreSQL: {e}")
            return False

    def disconnect(self):
        """Close PostgreSQL connection."""
        if self.conn:
            self.conn.close()
            self.conn = None

    def check_connection(self) -> bool:
        """Check if connection is active."""
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT 1")
                return True
        except Exception:
            return False

    def table_exists(self, table_name: str) -> bool:
        """Check if a table exists in PostgreSQL."""
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = %s)",
                    (table_name,)
                )
                return cur.fetchone()[0]
        except Exception as e:
            logger.error(f"Error checking table existence {table_name}: {e}")
            return False

    def create_table_from_sqlite_info(self, pg_table_name: str,
                                     sqlite_columns: List[Dict[str, Any]]) -> bool:
        """Create a table in PostgreSQL based on SQLite schema."""

        # Map SQLite types to PostgreSQL types
        type_mapping = {
            'INTEGER': 'BIGINT',
            'INT': 'BIGINT',
            'BIGINT': 'BIGINT',
            'REAL': 'DOUBLE PRECISION',
            'FLOAT': 'DOUBLE PRECISION',
            'DOUBLE': 'DOUBLE PRECISION',
            'TEXT': 'TEXT',
            'VARCHAR': 'TEXT',
            'STRING': 'TEXT',
            'DATETIME': 'BIGINT',  # Store as milliseconds timestamp
            'TIMESTAMP': 'BIGINT',
        }

        # Build column definitions
        column_defs = []
        for col in sqlite_columns:
            col_name = col['name']
            sqlite_type = col['type'].upper().split('(')[0]  # Remove size constraints

            # Map type
            pg_type = type_mapping.get(sqlite_type, 'TEXT')

            # Handle primary key
            pk_def = "PRIMARY KEY" if col['pk'] else ""

            # Handle NOT NULL
            not_null = "NOT NULL" if col['notnull'] and not col['pk'] else ""

            column_defs.append(f'"{col_name}" {pg_type} {pk_def} {not_null}'.strip())

        # Create table SQL
        create_sql = f'CREATE TABLE IF NOT EXISTS "{pg_table_name}" (\n    '
        create_sql += ',\n    '.join(column_defs)
        create_sql += '\n)'

        try:
            with self.conn.cursor() as cur:
                cur.execute(create_sql)
            logger.info(f"Created table {pg_table_name}")
            return True
        except Exception as e:
            logger.error(f"Error creating table {pg_table_name}: {e}")
            return False

    def insert_data(self, pg_table_name: str, data: List[Dict[str, Any]]) -> int:
        """Insert data into PostgreSQL table."""
        if not data:
            return 0

        # Get column names from first row
        columns = list(data[0].keys())
        placeholders = ', '.join(['%s'] * len(columns))
        col_names = ', '.join([f'"{col}"' for col in columns])

        insert_sql = f'INSERT INTO "{pg_table_name}" ({col_names}) VALUES ({placeholders})'

        inserted = 0
        try:
            with self.conn.cursor() as cur:
                for row in data:
                    values = [row.get(col) for col in columns]
                    cur.execute(insert_sql, values)
                    inserted += 1

            if inserted > 0:
                logger.info(f"Inserted {inserted} rows into {pg_table_name}")

            return inserted
        except Exception as e:
            logger.error(f"Error inserting into {pg_table_name}: {e}")
            return inserted

    def get_table_row_count(self, table_name: str) -> int:
        """Get row count for a table."""
        try:
            with self.conn.cursor() as cur:
                cur.execute(f'SELECT COUNT(*) FROM "{table_name}"')
                return cur.fetchone()[0]
        except Exception as e:
            logger.error(f"Error counting rows in {table_name}: {e}")
            return 0


class MigrationHelper:
    """Main migration helper class."""

    def __init__(self):
        load_dotenv()
        self.pg_host = os.getenv("PG_HOST", "localhost")
        self.pg_port = int(os.getenv("PG_PORT", "5432"))
        self.pg_database = os.getenv("PG_DB", "crypto_data")
        self.pg_user = os.getenv("PG_USER", "postgres")
        self.pg_password = os.getenv("PG_PASSWORD", "")

        # SQLite paths
        self.sqlite_crypto_path = os.getenv("SQLITE_CRYPTO_PATH",
                                           DEFAULT_SQLITE_PATHS['crypto_data'])
        self.sqlite_trader_path = os.getenv("SQLITE_TRADER_PATH",
                                           DEFAULT_SQLITE_PATHS['top_trader_data'])

    def check_sqlite_databases(self) -> Dict[str, Any]:
        """Check SQLite databases and report statistics."""
        results = {}

        # Check crypto data database
        if os.path.exists(self.sqlite_crypto_path):
            logger.info(f"Checking SQLite database: {self.sqlite_crypto_path}")
            with SQLiteInspector(self.sqlite_crypto_path) as inspector:
                stats = inspector.get_all_table_stats()
                categorized = inspector.categorize_tables()

                results['crypto_data'] = {
                    'path': self.sqlite_crypto_path,
                    'exists': True,
                    'table_count': len(stats),
                    'table_stats': stats,
                    'categorized_tables': categorized,
                    'total_rows': sum(stats.get(table, {}).get('row_count', 0)
                                    for table in stats)
                }

                # Print summary
                print(f"\n=== Crypto Data Database ===")
                print(f"Path: {self.sqlite_crypto_path}")
                print(f"Total tables: {len(stats)}")
                print(f"Total rows: {results['crypto_data']['total_rows']:,}")

                print(f"\nTable categories:")
                for category, tables in categorized.items():
                    if tables:
                        print(f"  {category}: {len(tables)} tables")
                        if category in ['kline_5m', 'kline_1h', 'kline_1d']:
                            row_count = sum(
                                stats.get(table, {}).get('row_count', 0)
                                for table in tables
                            )
                            print(f"    Total rows: {row_count:,}")

        else:
            logger.warning(f"SQLite database not found: {self.sqlite_crypto_path}")
            results['crypto_data'] = {
                'path': self.sqlite_crypto_path,
                'exists': False
            }

        # Check top trader data database
        if os.path.exists(self.sqlite_trader_path):
            logger.info(f"Checking SQLite database: {self.sqlite_trader_path}")
            with SQLiteInspector(self.sqlite_trader_path) as inspector:
                stats = inspector.get_all_table_stats()

                results['top_trader_data'] = {
                    'path': self.sqlite_trader_path,
                    'exists': True,
                    'table_count': len(stats),
                    'table_stats': stats,
                    'total_rows': sum(stats.get(table, {}).get('row_count', 0)
                                    for table in stats)
                }

                print(f"\n=== Top Trader Data Database ===")
                print(f"Path: {self.sqlite_trader_path}")
                print(f"Total tables: {len(stats)}")
                print(f"Total rows: {results['top_trader_data']['total_rows']:,}")

        else:
            logger.warning(f"SQLite database not found: {self.sqlite_trader_path}")
            results['top_trader_data'] = {
                'path': self.sqlite_trader_path,
                'exists': False
            }

        return results

    def check_postgresql_connection(self) -> bool:
        """Check PostgreSQL connection and existing tables."""
        try:
            with PostgreSQLConnector(
                self.pg_host, self.pg_port, self.pg_database,
                self.pg_user, self.pg_password
            ) as pg:
                if not pg.check_connection():
                    logger.error("PostgreSQL connection test failed")
                    return False

                # Check for existing tables
                logger.info("Checking existing PostgreSQL tables...")

                # Look for K-line tables
                with pg.conn.cursor() as cur:
                    cur.execute(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema='public' AND table_name LIKE 'K5m%' "
                        "LIMIT 5"
                    )
                    k5m_tables = [row[0] for row in cur.fetchall()]

                    cur.execute(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema='public' AND table_name LIKE 'FR%' "
                        "LIMIT 5"
                    )
                    fr_tables = [row[0] for row in cur.fetchall()]

                print(f"\n=== PostgreSQL Connection ===")
                print(f"Host: {self.pg_host}:{self.pg_port}")
                print(f"Database: {self.pg_database}")
                print(f"User: {self.pg_user}")
                print(f"\nExisting tables (sample):")
                print(f"  K5m* tables: {len(k5m_tables)} found")
                if k5m_tables:
                    print(f"    Sample: {', '.join(k5m_tables[:3])}")
                print(f"  FR* tables: {len(fr_tables)} found")
                if fr_tables:
                    print(f"    Sample: {', '.join(fr_tables[:3])}")

                return True

        except Exception as e:
            logger.error(f"PostgreSQL connection failed: {e}")
            print(f"\n=== PostgreSQL Connection Failed ===")
            print(f"Error: {e}")
            print(f"\nPlease check your .env configuration:")
            print(f"  PG_HOST={self.pg_host}")
            print(f"  PG_PORT={self.pg_port}")
            print(f"  PG_DB={self.pg_database}")
            print(f"  PG_USER={self.pg_user}")
            print(f"  PG_PASSWORD={'*' * len(self.pg_password) if self.pg_password else '(empty)'}")
            return False

    def migrate_kline_table(self, sqlite_table: str, pg_table: str,
                           sqlite_path: str, pg_connector: PostgreSQLConnector,
                           batch_size: int = 1000, dry_run: bool = False) -> Dict[str, Any]:
        """Migrate a K-line table from SQLite to PostgreSQL."""
        result = {
            'sqlite_table': sqlite_table,
            'pg_table': pg_table,
            'migrated_rows': 0,
            'errors': [],
            'skipped': False
        }

        logger.info(f"Migrating {sqlite_table} -> {pg_table}")

        try:
            # Check if table already exists in PostgreSQL
            if pg_connector.table_exists(pg_table):
                existing_rows = pg_connector.get_table_row_count(pg_table)
                logger.info(f"Table {pg_table} already exists with {existing_rows} rows")

                if not dry_run:
                    # In real migration, we might want to skip or update
                    # For now, we'll skip if table exists
                    result['skipped'] = True
                    result['message'] = f"Table already exists with {existing_rows} rows"
                    return result

            # Connect to SQLite
            with sqlite3.connect(sqlite_path) as sqlite_conn:
                sqlite_conn.row_factory = sqlite3.Row
                cursor = sqlite_conn.cursor()

                # Get table schema
                cursor.execute(f"PRAGMA table_info({sqlite_table})")
                columns = cursor.fetchall()

                if dry_run:
                    result['dry_run'] = True
                    result['column_count'] = len(columns)
                    # Get row count for dry run
                    cursor.execute(f"SELECT COUNT(*) FROM {sqlite_table}")
                    result['sqlite_rows'] = cursor.fetchone()[0]
                    return result

                # Create table in PostgreSQL
                column_info = []
                for col in columns:
                    column_info.append({
                        'name': col[1],
                        'type': col[2],
                        'notnull': col[3],
                        'default': col[4],
                        'pk': col[5]
                    })

                if not pg_connector.create_table_from_sqlite_info(pg_table, column_info):
                    result['errors'].append("Failed to create PostgreSQL table")
                    return result

                # Migrate data in batches
                cursor.execute(f"SELECT * FROM {sqlite_table}")

                batch = []
                migrated = 0

                while True:
                    rows = cursor.fetchmany(batch_size)
                    if not rows:
                        break

                    # Convert to dictionaries
                    batch_data = []
                    for row in rows:
                        batch_data.append(dict(row))

                    # Insert batch
                    inserted = pg_connector.insert_data(pg_table, batch_data)
                    migrated += inserted

                    if inserted < len(batch_data):
                        result['errors'].append(f"Batch insert partial: {inserted}/{len(batch_data)}")

                    # Progress logging
                    if migrated % (batch_size * 10) == 0:
                        logger.info(f"  Migrated {migrated:,} rows...")

                result['migrated_rows'] = migrated
                logger.info(f"Completed migration of {sqlite_table}: {migrated:,} rows")

        except Exception as e:
            logger.error(f"Error migrating {sqlite_table}: {e}")
            result['errors'].append(str(e))

        return result

    def migrate_funding_rate_data(self, sqlite_path: str, pg_connector: PostgreSQLConnector,
                                 dry_run: bool = False) -> Dict[str, Any]:
        """Migrate funding rate data from SQLite to PostgreSQL."""
        result = {
            'total_migrated': 0,
            'tables_created': 0,
            'errors': [],
            'details': {}
        }

        logger.info("Migrating funding rate data...")

        try:
            with sqlite3.connect(sqlite_path) as sqlite_conn:
                sqlite_conn.row_factory = sqlite3.Row
                cursor = sqlite_conn.cursor()

                # Check if funding_rate_history table exists
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='funding_rate_history'"
                )
                if not cursor.fetchone():
                    logger.warning("funding_rate_history table not found in SQLite")
                    result['errors'].append("funding_rate_history table not found")
                    return result

                # Get unique symbols
                cursor.execute("SELECT DISTINCT symbol FROM funding_rate_history")
                symbols = [row[0] for row in cursor.fetchall()]

                logger.info(f"Found {len(symbols)} unique symbols in funding rate data")

                if dry_run:
                    result['dry_run'] = True
                    result['symbol_count'] = len(symbols)
                    result['symbols_sample'] = symbols[:10]

                    # Get total row count
                    cursor.execute("SELECT COUNT(*) FROM funding_rate_history")
                    result['total_rows'] = cursor.fetchone()[0]
                    return result

                # Migrate each symbol to its own table
                for symbol in symbols:
                    pg_table_name = f"FR{symbol}"

                    # Check if table already exists
                    if pg_connector.table_exists(pg_table_name):
                        existing_rows = pg_connector.get_table_row_count(pg_table_name)
                        logger.info(f"Table {pg_table_name} already exists with {existing_rows} rows")
                        result['details'][symbol] = {
                            'skipped': True,
                            'existing_rows': existing_rows
                        }
                        continue

                    # Get data for this symbol
                    cursor.execute(
                        "SELECT funding_time, funding_rate, mark_price FROM funding_rate_history "
                        "WHERE symbol = ? ORDER BY funding_time",
                        (symbol,)
                    )

                    rows = cursor.fetchall()
                    if not rows:
                        continue

                    # Create table in PostgreSQL
                    column_info = [
                        {'name': 'funding_time', 'type': 'BIGINT', 'notnull': 1, 'default': None, 'pk': 1},
                        {'name': 'funding_rate', 'type': 'REAL', 'notnull': 0, 'default': None, 'pk': 0},
                        {'name': 'mark_price', 'type': 'REAL', 'notnull': 0, 'default': None, 'pk': 0}
                    ]

                    if not pg_connector.create_table_from_sqlite_info(pg_table_name, column_info):
                        result['errors'].append(f"Failed to create table {pg_table_name}")
                        continue

                    # Insert data
                    data = []
                    for row in rows:
                        # Convert funding_time to milliseconds if it's not already
                        funding_time = row[0]
                        if funding_time < 1e12:  # Likely seconds, convert to milliseconds
                            funding_time *= 1000

                        data.append({
                            'funding_time': funding_time,
                            'funding_rate': row[1],
                            'mark_price': row[2]
                        })

                    inserted = pg_connector.insert_data(pg_table_name, data)

                    result['details'][symbol] = {
                        'rows_migrated': inserted,
                        'pg_table': pg_table_name
                    }
                    result['total_migrated'] += inserted
                    result['tables_created'] += 1

                    logger.info(f"  Migrated {symbol}: {inserted} rows -> {pg_table_name}")

        except Exception as e:
            logger.error(f"Error migrating funding rate data: {e}")
            result['errors'].append(str(e))

        return result

    def migrate_all(self, dry_run: bool = False, tables_limit: int = None) -> Dict[str, Any]:
        """Migrate all data from SQLite to PostgreSQL."""
        overall_result = {
            'dry_run': dry_run,
            'crypto_data_migrated': False,
            'top_trader_migrated': False,
            'tables_migrated': 0,
            'total_rows_migrated': 0,
            'errors': [],
            'details': {}
        }

        if dry_run:
            logger.info("=== DRY RUN MODE ===")

        # Check PostgreSQL connection
        try:
            with PostgreSQLConnector(
                self.pg_host, self.pg_port, self.pg_database,
                self.pg_user, self.pg_password
            ) as pg_connector:

                if not pg_connector.check_connection():
                    overall_result['errors'].append("PostgreSQL connection failed")
                    return overall_result

                # Migrate crypto data
                if os.path.exists(self.sqlite_crypto_path):
                    logger.info(f"\n=== Migrating Crypto Data ===")

                    with SQLiteInspector(self.sqlite_crypto_path) as inspector:
                        categorized = inspector.categorize_tables()

                        # Migrate K-line tables
                        for category, sqlite_tables in categorized.items():
                            if category in ['kline_5m', 'kline_1h', 'kline_1d']:
                                prefix = category.replace('kline_', '').replace('_', '')
                                prefix = prefix.upper()  # K5M -> K5m (will be capitalized properly)

                                for sqlite_table in sqlite_tables[:tables_limit] if tables_limit else sqlite_tables:
                                    # Extract symbol and create PostgreSQL table name
                                    symbol = inspector.extract_symbol_from_table(sqlite_table)
                                    if not symbol:
                                        logger.warning(f"Could not extract symbol from {sqlite_table}")
                                        continue

                                    # Correct prefix mapping
                                    if prefix == '5M':
                                        pg_prefix = 'K5m'
                                    elif prefix == '1H':
                                        pg_prefix = 'K1h'
                                    elif prefix == '1D':
                                        pg_prefix = 'K1d'
                                    else:
                                        pg_prefix = f'K{prefix}'

                                    pg_table = f"{pg_prefix}{symbol}"

                                    # Migrate table
                                    result = self.migrate_kline_table(
                                        sqlite_table, pg_table, self.sqlite_crypto_path,
                                        pg_connector, dry_run=dry_run
                                    )

                                    overall_result['details'][sqlite_table] = result

                                    if not result.get('skipped', False) and not dry_run:
                                        if result.get('migrated_rows', 0) > 0:
                                            overall_result['tables_migrated'] += 1
                                            overall_result['total_rows_migrated'] += result['migrated_rows']

                                    if result.get('errors'):
                                        overall_result['errors'].extend([
                                            f"{sqlite_table}: {err}" for err in result['errors']
                                        ])

                        # Migrate funding rate data
                        if 'funding_rate' in categorized and categorized['funding_rate']:
                            logger.info("\n=== Migrating Funding Rate Data ===")
                            fr_result = self.migrate_funding_rate_data(
                                self.sqlite_crypto_path, pg_connector, dry_run=dry_run
                            )

                            overall_result['funding_rate_migration'] = fr_result

                            if not dry_run:
                                overall_result['tables_migrated'] += fr_result.get('tables_created', 0)
                                overall_result['total_rows_migrated'] += fr_result.get('total_migrated', 0)

                            if fr_result.get('errors'):
                                overall_result['errors'].extend([
                                    f"FundingRate: {err}" for err in fr_result['errors']
                                ])

                    overall_result['crypto_data_migrated'] = True

                else:
                    logger.warning(f"Crypto data SQLite file not found: {self.sqlite_crypto_path}")
                    overall_result['errors'].append(f"Crypto data file not found")

        except Exception as e:
            logger.error(f"Migration failed: {e}")
            overall_result['errors'].append(str(e))

        return overall_result

    def generate_env_template(self) -> str:
        """Generate a template .env file for PostgreSQL configuration."""
        template = """# PostgreSQL Configuration
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
"""
        return template

    def verify_migration(self) -> Dict[str, Any]:
        """Verify that migration was successful."""
        verification = {
            'passed': True,
            'checks': [],
            'errors': []
        }

        try:
            with PostgreSQLConnector(
                self.pg_host, self.pg_port, self.pg_database,
                self.pg_user, self.pg_password
            ) as pg:

                # Check for essential table patterns
                essential_patterns = ['K5m%', 'K1h%', 'K1d%', 'FR%']

                for pattern in essential_patterns:
                    with pg.conn.cursor() as cur:
                        cur.execute(
                            "SELECT COUNT(*) FROM information_schema.tables "
                            "WHERE table_schema='public' AND table_name LIKE %s",
                            (pattern,)
                        )
                        count = cur.fetchone()[0]

                        check = {
                            'pattern': pattern,
                            'table_count': count,
                            'status': 'PASS' if count > 0 else 'FAIL'
                        }

                        verification['checks'].append(check)

                        if count == 0:
                            verification['passed'] = False
                            verification['errors'].append(f"No tables found for pattern: {pattern}")

                # Sample data check
                for check in verification['checks']:
                    if check['table_count'] > 0:
                        pattern = check['pattern']
                        with pg.conn.cursor() as cur:
                            cur.execute(
                                "SELECT table_name FROM information_schema.tables "
                                "WHERE table_schema='public' AND table_name LIKE %s "
                                "LIMIT 1",
                                (pattern,)
                            )
                            sample_table = cur.fetchone()

                            if sample_table:
                                table_name = sample_table[0]
                                # Check if table has data
                                try:
                                    cur.execute(f'SELECT COUNT(*) FROM "{table_name}" LIMIT 1')
                                    row_count = cur.fetchone()[0]
                                    check['sample_table'] = table_name
                                    check['sample_rows'] = row_count
                                except Exception as e:
                                    check['sample_error'] = str(e)

        except Exception as e:
            verification['passed'] = False
            verification['errors'].append(f"Verification failed: {e}")

        return verification


def main():
    parser = argparse.ArgumentParser(
        description="SQLite to PostgreSQL Migration Helper for duo-moonshot"
    )

    subparsers = parser.add_subparsers(dest='command', help='Command to run')

    # Check SQLite command
    check_sqlite_parser = subparsers.add_parser('check-sqlite',
                                               help='Check SQLite databases')

    # Check PostgreSQL command
    check_pg_parser = subparsers.add_parser('check-postgresql',
                                           help='Check PostgreSQL connection')

    # Migrate command
    migrate_parser = subparsers.add_parser('migrate', help='Migrate data')
    migrate_parser.add_argument('--dry-run', action='store_true',
                               help='Dry run without making changes')
    migrate_parser.add_argument('--tables-limit', type=int,
                               help='Limit number of tables to migrate (for testing)')
    migrate_parser.add_argument('--all', action='store_true',
                               help='Migrate all data (requires confirmation)')

    # Verify command
    verify_parser = subparsers.add_parser('verify', help='Verify migration')

    # Env template command
    env_parser = subparsers.add_parser('env-template', help='Generate .env template')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    helper = MigrationHelper()

    if args.command == 'check-sqlite':
        print("Checking SQLite databases...")
        helper.check_sqlite_databases()

    elif args.command == 'check-postgresql':
        print("Checking PostgreSQL connection...")
        helper.check_postgresql_connection()

    elif args.command == 'migrate':
        if args.all:
            confirm = input("Are you sure you want to migrate ALL data? This may take a long time. (y/N): ")
            if confirm.lower() != 'y':
                print("Migration cancelled.")
                return

        print(f"Starting migration (dry-run: {args.dry_run})...")
        result = helper.migrate_all(dry_run=args.dry_run, tables_limit=args.tables_limit)

        print(f"\n=== Migration Summary ===")
        print(f"Dry run: {result['dry_run']}")
        print(f"Tables migrated: {result['tables_migrated']}")
        print(f"Total rows migrated: {result['total_rows_migrated']:,}")

        if result['errors']:
            print(f"\nErrors ({len(result['errors'])}):")
            for error in result['errors'][:10]:  # Show first 10 errors
                print(f"  - {error}")
            if len(result['errors']) > 10:
                print(f"  ... and {len(result['errors']) - 10} more errors")

        if args.dry_run:
            print("\nThis was a dry run. No changes were made to the database.")
            print("To perform actual migration, run without --dry-run flag.")

    elif args.command == 'verify':
        print("Verifying migration...")
        verification = helper.verify_migration()

        print(f"\n=== Migration Verification ===")
        print(f"Overall status: {'PASS' if verification['passed'] else 'FAIL'}")

        for check in verification['checks']:
            print(f"\n{check['pattern']}:")
            print(f"  Tables found: {check['table_count']}")
            print(f"  Status: {check['status']}")
            if 'sample_table' in check:
                print(f"  Sample table: {check['sample_table']}")
                print(f"  Sample rows: {check['sample_rows']:,}")

        if verification['errors']:
            print(f"\nErrors:")
            for error in verification['errors']:
                print(f"  - {error}")

    elif args.command == 'env-template':
        print("Generating .env template...")
        template = helper.generate_env_template()
        print("\nCopy this to a .env file in your project root:")
        print("=" * 60)
        print(template)
        print("=" * 60)


if __name__ == '__main__':
    main()
