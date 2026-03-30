"""PostgreSQL database connection for duo-moonshot.

Connects to local PostgreSQL server for market data.
Configure via .env: PG_HOST, PG_PORT, PG_DB, PG_USER, PG_PASSWORD
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


class PostgresDatabase:
    """PostgreSQL database manager."""

    def __init__(self) -> None:
        self._conn = None
        self.host = os.getenv("PG_HOST", "localhost")
        self.port = int(os.getenv("PG_PORT", "5432"))
        self.database = os.getenv("PG_DB", "crypto_data")
        self.user = os.getenv("PG_USER", "postgres")
        self.password = os.getenv("PG_PASSWORD", "")

    def connect(self) -> PostgresDatabase:
        """Establish database connection."""
        try:
            import psycopg2
            self._conn = psycopg2.connect(
                host=self.host,
                port=self.port,
                database=self.database,
                user=self.user,
                password=self.password,
            )
            self._conn.autocommit = True
        except ImportError:
            raise ImportError("psycopg2 not installed. Run: pip install psycopg2-binary")
        return self

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> PostgresDatabase:
        return self.connect()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    @property
    def conn(self):
        if not self._conn:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._conn

    def check_connection(self) -> bool:
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT 1")
                return True
        except Exception:
            return False


def get_postgres_db() -> PostgresDatabase:
    """Get a configured PostgreSQL database instance."""
    return PostgresDatabase()
