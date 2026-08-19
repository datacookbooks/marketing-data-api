"""SQLite connection, schema, and read helpers."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Iterator


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_state (
    table_name TEXT NOT NULL,
    business_key TEXT NOT NULL,
    row_hash TEXT NOT NULL,
    PRIMARY KEY (table_name, business_key)
);

CREATE TABLE IF NOT EXISTS raw_deliveries (
    _raw_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL,
    generated_for_date TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    delivered_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_raw_deliveries_table_cursor
    ON raw_deliveries (table_name, _raw_row_id);

CREATE TABLE IF NOT EXISTS generation_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_date TEXT NOT NULL UNIQUE,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    status TEXT NOT NULL,
    changed_clean_rows INTEGER NOT NULL,
    delivered_raw_rows INTEGER NOT NULL,
    table_counts_json TEXT NOT NULL
);
"""


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteStore:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)

    def connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.database_path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connection() as connection:
            connection.executescript(SCHEMA_SQL)
            connection.commit()

    def health(self) -> dict:
        with self.connection() as connection:
            connection.execute("SELECT 1").fetchone()
            raw_rows = connection.execute(
                "SELECT COUNT(*) AS count FROM raw_deliveries"
            ).fetchone()["count"]
            last_generated = self._metadata_value(connection, "last_generated_date")
        return {
            "status": "healthy",
            "raw_rows": raw_rows,
            "last_generated_date": last_generated,
        }

    @staticmethod
    def _metadata_value(
        connection: sqlite3.Connection, key: str
    ) -> str | None:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def metadata_value(self, key: str) -> str | None:
        with self.connection() as connection:
            return self._metadata_value(connection, key)

    def state_hashes(self, table_name: str) -> dict[str, str]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT business_key, row_hash FROM source_state WHERE table_name = ?",
                (table_name,),
            ).fetchall()
        return {row["business_key"]: row["row_hash"] for row in rows}

    def table_summary(self, table_names: tuple[str, ...]) -> list[dict]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    table_name,
                    COUNT(*) AS row_count,
                    MIN(_raw_row_id) AS first_raw_row_id,
                    MAX(_raw_row_id) AS last_raw_row_id,
                    MAX(generated_for_date) AS latest_generation_date
                FROM raw_deliveries
                GROUP BY table_name
                """
            ).fetchall()
        by_name = {row["table_name"]: dict(row) for row in rows}
        return [
            by_name.get(
                table_name,
                {
                    "table_name": table_name,
                    "row_count": 0,
                    "first_raw_row_id": None,
                    "last_raw_row_id": None,
                    "latest_generation_date": None,
                },
            )
            for table_name in table_names
        ]

    def raw_page(
        self, table_name: str, since: int, limit: int
    ) -> tuple[list[dict], bool]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT _raw_row_id, generated_for_date, payload_json
                FROM raw_deliveries
                WHERE table_name = ? AND _raw_row_id > ?
                ORDER BY _raw_row_id
                LIMIT ?
                """,
                (table_name, since, limit + 1),
            ).fetchall()

        has_more = len(rows) > limit
        selected = rows[:limit]
        data: list[dict] = []
        for row in selected:
            payload = json.loads(row["payload_json"])
            data.append(
                {
                    "_raw_row_id": row["_raw_row_id"],
                    "_generated_for_date": row["generated_for_date"],
                    **payload,
                }
            )
        return data, has_more

    def status(self, table_names: tuple[str, ...]) -> dict:
        with self.connection() as connection:
            last_run = connection.execute(
                """
                SELECT target_date, completed_at, changed_clean_rows,
                       delivered_raw_rows, table_counts_json
                FROM generation_runs
                ORDER BY run_id DESC
                LIMIT 1
                """
            ).fetchone()
            last_generated = self._metadata_value(connection, "last_generated_date")

        run = None
        if last_run:
            run = dict(last_run)
            run["table_counts"] = json.loads(run.pop("table_counts_json"))
        return {
            "last_generated_date": last_generated,
            "last_run": run,
            "tables": self.table_summary(table_names),
        }

