"""Generate deterministic snapshots and append changed source rows to SQLite."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
import hashlib
import json
import threading
from typing import Any

import numpy as np
import pandas as pd

from generator.config import DEFAULT_CONFIG, ProjectConfig
from generator.daily_generator import BUSINESS_KEYS
from generator.historical_seed import TABLE_ORDER, generate_historical_data
from generator.messiness import apply_messiness
from generator.validation import validate_clean_tables

from .db import SQLiteStore, utc_now_text


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and np.isnan(value):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _canonical_record(record: dict) -> dict:
    return {key: _json_value(value) for key, value in record.items()}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _row_hash(record: dict) -> str:
    return hashlib.sha256(_canonical_json(record).encode("utf-8")).hexdigest()


def _messiness_seed(base_seed: int, target_date: date) -> int:
    payload = f"{base_seed}|{target_date.isoformat()}|source-delivery".encode("utf-8")
    return int.from_bytes(hashlib.blake2b(payload, digest_size=4).digest(), "big")


class GenerationService:
    """Owns initialization, catch-up generation, and idempotent daily updates."""

    def __init__(
        self,
        store: SQLiteStore,
        config: ProjectConfig = DEFAULT_CONFIG,
    ):
        self.store = store
        self.config = config
        self._lock = threading.Lock()

    def initialize_database(self) -> None:
        self.store.initialize()

    def initialize_and_backfill(self) -> list[dict]:
        self.initialize_database()
        today = datetime.now(timezone.utc).date()
        seed_target = min(self.config.historical_cutoff_date, today)
        results = [self.generate_through(seed_target)]
        if today > seed_target:
            results.append(self.generate_through(today))
        return results

    def generate_through(self, through_date: date | str) -> dict:
        target = pd.Timestamp(through_date).date()
        today = datetime.now(timezone.utc).date()
        if target < self.config.history_start_date:
            raise ValueError(
                f"Target date must be on or after {self.config.history_start_date}."
            )
        if target > today:
            raise ValueError("Future generation is disabled.")

        with self._lock:
            return self._generate_locked(target)

    def _generate_locked(self, target: date) -> dict:
        last_generated_text = self.store.metadata_value("last_generated_date")
        if last_generated_text and target <= date.fromisoformat(last_generated_text):
            return {
                "status": "already_current",
                "target_date": target.isoformat(),
                "last_generated_date": last_generated_text,
                "changed_clean_rows": 0,
                "delivered_raw_rows": 0,
                "table_counts": {},
            }

        started_at = utc_now_text()
        simulation = generate_historical_data(
            start_date=self.config.history_start_date,
            end_date=target,
            config=self.config,
        )

        validation = validate_clean_tables(simulation.clean_tables)
        failures = validation[validation["status"] != "PASS"]
        if not failures.empty:
            messages = failures[["check_name", "observed"]].to_dict(orient="records")
            raise RuntimeError(f"Clean-data validation failed: {messages}")

        delta_tables: dict[str, pd.DataFrame] = {}
        state_updates: dict[str, list[tuple[str, str]]] = {}
        clean_counts: dict[str, int] = {}

        for table_name in TABLE_ORDER:
            frame = simulation.clean_tables[table_name]
            keys = BUSINESS_KEYS[table_name]
            old_hashes = self.store.state_hashes(table_name)
            current_keys: set[str] = set()
            changed_positions: list[int] = []
            updates: list[tuple[str, str]] = []

            for position, record in enumerate(frame.to_dict(orient="records")):
                normalized = _canonical_record(record)
                business_key = _canonical_json([normalized[key] for key in keys])
                current_keys.add(business_key)
                digest = _row_hash(normalized)
                if old_hashes.get(business_key) != digest:
                    changed_positions.append(position)
                    updates.append((business_key, digest))

            removed_keys = set(old_hashes) - current_keys
            if removed_keys:
                raise RuntimeError(
                    f"{table_name} lost {len(removed_keys)} business keys; "
                    "generation stopped to protect append-only history."
                )

            delta_tables[table_name] = frame.iloc[changed_positions].reset_index(drop=True)
            state_updates[table_name] = updates
            clean_counts[table_name] = len(changed_positions)

        messy_config = replace(
            self.config,
            random_seed=_messiness_seed(self.config.random_seed, target),
        )
        raw_tables = apply_messiness(delta_tables, messy_config)
        raw_counts = {table_name: len(frame) for table_name, frame in raw_tables.items()}

        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_last = self.store._metadata_value(connection, "last_generated_date")
            if current_last != last_generated_text:
                connection.rollback()
                raise RuntimeError(
                    "Generation state changed during this run. Retry the request."
                )

            for table_name in TABLE_ORDER:
                updates = [
                    (table_name, business_key, digest)
                    for business_key, digest in state_updates[table_name]
                ]
                connection.executemany(
                    """
                    INSERT INTO source_state (table_name, business_key, row_hash)
                    VALUES (?, ?, ?)
                    ON CONFLICT (table_name, business_key)
                    DO UPDATE SET row_hash = excluded.row_hash
                    """,
                    updates,
                )

                delivery_rows: list[tuple[str, str, str, str]] = []
                for record in raw_tables[table_name].to_dict(orient="records"):
                    normalized = _canonical_record(record)
                    normalized.pop("_raw_row_id", None)
                    delivery_rows.append(
                        (
                            table_name,
                            target.isoformat(),
                            _canonical_json(normalized),
                            utc_now_text(),
                        )
                    )
                connection.executemany(
                    """
                    INSERT INTO raw_deliveries (
                        table_name, generated_for_date, payload_json, delivered_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    delivery_rows,
                )

            completed_at = utc_now_text()
            total_clean = sum(clean_counts.values())
            total_raw = sum(raw_counts.values())
            table_counts = {
                table_name: {
                    "changed_clean_rows": clean_counts[table_name],
                    "delivered_raw_rows": raw_counts[table_name],
                }
                for table_name in TABLE_ORDER
            }
            connection.execute(
                """
                INSERT INTO metadata (key, value, updated_at)
                VALUES ('last_generated_date', ?, ?)
                ON CONFLICT (key)
                DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (target.isoformat(), completed_at),
            )
            connection.execute(
                """
                INSERT INTO generation_runs (
                    target_date, started_at, completed_at, status,
                    changed_clean_rows, delivered_raw_rows, table_counts_json
                ) VALUES (?, ?, ?, 'completed', ?, ?, ?)
                """,
                (
                    target.isoformat(),
                    started_at,
                    completed_at,
                    total_clean,
                    total_raw,
                    _canonical_json(table_counts),
                ),
            )
            connection.commit()

        return {
            "status": "generated",
            "target_date": target.isoformat(),
            "last_generated_date": target.isoformat(),
            "changed_clean_rows": total_clean,
            "delivered_raw_rows": total_raw,
            "table_counts": table_counts,
        }

