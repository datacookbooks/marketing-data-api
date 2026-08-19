"""Apply controlled source-system defects after clean truth is generated."""

from __future__ import annotations

import hashlib
from typing import Dict

import numpy as np
import pandas as pd

try:
    from .config import DEFAULT_CONFIG, ProjectConfig
except ImportError:
    from config import DEFAULT_CONFIG, ProjectConfig


def _seed(config: ProjectConfig, *parts: object) -> int:
    payload = "|".join(str(p) for p in (config.random_seed, *parts)).encode()
    return int.from_bytes(hashlib.blake2b(payload, digest_size=4).digest(), "big")


def _as_source_text(frame: pd.DataFrame) -> pd.DataFrame:
    raw = frame.copy(deep=True)
    for column in raw.columns:
        raw[column] = raw[column].map(lambda value: None if pd.isna(value) else str(value))
    return raw


def _add_delivery_ids(frame: pd.DataFrame, table_name: str) -> pd.DataFrame:
    output = frame.copy()
    output.insert(0, "_raw_row_id", [f"RAW-{table_name}-{i:09d}" for i in range(len(output))])
    return output


def _sample_indices(rng: np.random.Generator, length: int, rate: float) -> np.ndarray:
    """Select each row independently so low rates still work on daily batches."""
    if not length or rate <= 0:
        return np.array([], dtype=int)
    return np.flatnonzero(rng.random(length) < min(rate, 1.0))


def apply_messiness(
    clean_tables: Dict[str, pd.DataFrame],
    config: ProjectConfig = DEFAULT_CONFIG,
) -> Dict[str, pd.DataFrame]:
    """Return source-shaped copies containing small, measurable defect rates."""
    raw_tables: Dict[str, pd.DataFrame] = {}
    d = config.defects
    for table_name, clean in clean_tables.items():
        rng = np.random.default_rng(_seed(config, "messy", table_name))
        raw = _as_source_text(clean)

        if table_name == "dim_customer":
            for idx in _sample_indices(rng, len(raw), d.blank_email_rate):
                raw.loc[idx, "email"] = " " if rng.random() < 0.5 else None
            for column in ("state", "experience_level", "initial_acquisition_channel"):
                for idx in _sample_indices(rng, len(raw), d.inconsistent_text_rate):
                    value = raw.loc[idx, column]
                    if value is not None:
                        raw.loc[idx, column] = f" {value.upper()} " if rng.random() < 0.5 else value.lower()

        if table_name == "dim_campaign":
            for idx in _sample_indices(rng, len(raw), 0.34):
                raw.loc[idx, "channel"] = f" {str(raw.loc[idx, 'channel']).upper()} "

        if table_name == "fact_campaign_daily":
            for idx in _sample_indices(rng, len(raw), d.missing_click_rate):
                raw.loc[idx, "clicks"] = ""
            for idx in _sample_indices(rng, len(raw), d.malformed_numeric_rate):
                raw.loc[idx, "spend"] = "unknown"
            correction_positions = _sample_indices(rng, len(raw), d.late_correction_rate)
            if correction_positions.size:
                corrections = raw.iloc[correction_positions].copy()
                for idx in corrections.index:
                    try:
                        corrections.loc[idx, "spend"] = f"{float(corrections.loc[idx, 'spend']) * rng.uniform(0.94, 1.08):.2f}"
                    except (TypeError, ValueError):
                        pass
                    corrections.loc[idx, "ingested_at"] = str(pd.Timestamp(clean.loc[idx, "ingested_at"]) + pd.Timedelta(days=2))
                raw = pd.concat([raw, corrections], ignore_index=True)

        if table_name == "fact_payment":
            for idx in _sample_indices(rng, len(raw), d.malformed_numeric_rate):
                raw.loc[idx, "amount"] = "not_available"

        if table_name == "fact_campaign_assignment":
            for idx in _sample_indices(rng, len(raw), d.unknown_campaign_rate):
                raw.loc[idx, "campaign_id"] = "9999"
            for idx in _sample_indices(rng, len(raw), d.inconsistent_text_rate):
                raw.loc[idx, "assignment_arm"] = f" {str(raw.loc[idx, 'assignment_arm']).upper()} "

        duplicate_positions = _sample_indices(rng, len(raw), d.duplicate_delivery_rate)
        if duplicate_positions.size:
            raw = pd.concat([raw, raw.iloc[duplicate_positions].copy()], ignore_index=True)
        exact_positions = _sample_indices(rng, len(raw), d.exact_duplicate_rate)
        if exact_positions.size:
            raw = pd.concat([raw, raw.iloc[exact_positions].copy()], ignore_index=True)

        raw = raw.sample(frac=1.0, random_state=_seed(config, "shuffle", table_name)).reset_index(drop=True)
        raw_tables[table_name] = _add_delivery_ids(raw, table_name)
    return raw_tables


if __name__ == "__main__":
    try:
        from .historical_seed import generate_historical_data
    except ImportError:
        from historical_seed import generate_historical_data
    clean_result = generate_historical_data()
    messy = apply_messiness(clean_result.clean_tables)
    for name, frame in messy.items():
        print(f"{name}: clean={len(clean_result.clean_tables[name]):,}, raw={len(frame):,}")
