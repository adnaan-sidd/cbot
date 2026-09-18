"""
Thin SQLite wrapper. Deliberately minimal in Phase 0: connection handling
and schema initialization only. CRUD helpers for specific tables are added
alongside the modules that need them (risk_engine, execution, etc.) in
later phases — Phase 0 just proves the persistence layer boots cleanly
and the schema applies.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


class DBManager:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, isolation_level=None)
            self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def init_schema(self) -> None:
        schema_path = Path(__file__).parent / "db_schema.sql"
        conn = self.connect()
        with schema_path.open("r", encoding="utf-8") as f:
            conn.executescript(f.read())

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "DBManager":
        self.init_schema()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
