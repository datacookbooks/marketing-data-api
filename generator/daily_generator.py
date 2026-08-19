"""Deterministic daily continuation and backfill logic.

For a portfolio-sized dataset, the safest continuation strategy is to rebuild
the authoritative deterministic simulation through the requested date, then
upsert by stable business key. This fills missed dates, updates formerly open
subscription periods, and cannot create a second real customer or payment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict

import pandas as pd

try:
    from .config import DEFAULT_CONFIG, ProjectConfig
    from .historical_seed import SimulationResult, generate_historical_data
except ImportError:
    from config import DEFAULT_CONFIG, ProjectConfig
    from historical_seed import SimulationResult, generate_historical_data


BUSINESS_KEYS = {
    "dim_plan": ["plan_id"],
    "dim_campaign": ["campaign_id"],
    "dim_customer": ["customer_id"],
    "fact_subscription_period": ["subscription_period_id"],
    "fact_payment": ["payment_id"],
    "fact_campaign_daily": ["metric_date", "campaign_id"],
    "fact_campaign_assignment": ["assignment_id"],
}


@dataclass
class DailyGenerationResult:
    simulation: SimulationResult
    new_row_counts: pd.DataFrame


def _key_set(frame: pd.DataFrame, keys: list[str]) -> set[tuple]:
    if frame.empty:
        return set()
    normalized = frame[keys].copy()
    for column in keys:
        if pd.api.types.is_datetime64_any_dtype(normalized[column]):
            normalized[column] = pd.to_datetime(normalized[column]).astype(str)
    return set(normalized.itertuples(index=False, name=None))


def extend_through_date(
    existing_clean_tables: Dict[str, pd.DataFrame],
    through_date: date | str,
    config: ProjectConfig = DEFAULT_CONFIG,
) -> DailyGenerationResult:
    """Backfill and extend clean data through an inclusive target date.

    Stable keys make this operation idempotent. The regenerated authoritative
    rows replace matching old keys, which is important when an open subscription
    period later receives an end timestamp.
    """
    target = pd.Timestamp(through_date).date()
    generated = generate_historical_data(config.history_start_date, target, config)
    counts: list[dict] = []
    for table_name, keys in BUSINESS_KEYS.items():
        old = existing_clean_tables.get(table_name, pd.DataFrame())
        new = generated.clean_tables[table_name]
        old_keys = _key_set(old, keys)
        new_keys = _key_set(new, keys)
        counts.append({
            "table_name": table_name,
            "rows_before": len(old),
            "rows_after": len(new),
            "new_business_keys": len(new_keys - old_keys),
            "keys_no_longer_present": len(old_keys - new_keys),
        })
    return DailyGenerationResult(generated, pd.DataFrame(counts))


def generate_missing_dates(
    existing_clean_tables: Dict[str, pd.DataFrame],
    through_date: date | str,
    config: ProjectConfig = DEFAULT_CONFIG,
) -> DailyGenerationResult:
    """Alias emphasizing that downtime gaps are backfilled automatically."""
    return extend_through_date(existing_clean_tables, through_date, config)


if __name__ == "__main__":
    baseline = generate_historical_data(end_date=DEFAULT_CONFIG.historical_cutoff_date)
    extended = extend_through_date(baseline.clean_tables, "2026-01-07")
    print(extended.new_row_counts.to_string(index=False))
