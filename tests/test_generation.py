from __future__ import annotations

from dataclasses import replace
from datetime import date

import pandas as pd

from generator.historical_seed import generate_historical_data
from generator.messiness import apply_messiness
from app.generation_service import (
    _canonical_json,
    _canonical_record,
    _row_hash,
    _state_hash,
)


def test_generation_is_idempotent(generation_service, small_config):
    first = generation_service.generate_through(
        small_config.historical_cutoff_date
    )
    assert first["status"] == "generated"
    assert first["delivered_raw_rows"] > 0

    before = generation_service.store.health()["raw_rows"]
    repeated = generation_service.generate_through(
        small_config.historical_cutoff_date
    )
    after = generation_service.store.health()["raw_rows"]

    assert repeated["status"] == "already_current"
    assert repeated["delivered_raw_rows"] == 0
    assert after == before


def test_next_date_appends_rows(generation_service, small_config):
    generation_service.generate_through(
        small_config.historical_cutoff_date
    )
    before = generation_service.store.health()["raw_rows"]

    result = generation_service.generate_through("2025-12-13")
    after = generation_service.store.health()["raw_rows"]

    assert result["status"] == "generated"
    assert result["changed_clean_rows"] > 0
    assert after > before


def test_campaign_daily_has_every_campaign_date(small_config):
    simulation = generate_historical_data(
        start_date=small_config.history_start_date,
        end_date=small_config.historical_cutoff_date,
        config=small_config,
    )
    daily = simulation.clean_tables["fact_campaign_daily"]

    expected_days = (
        small_config.historical_cutoff_date
        - small_config.history_start_date
    ).days + 1
    expected_rows = expected_days * len(small_config.campaigns)

    assert len(daily) == expected_rows
    assert not daily.duplicated(["metric_date", "campaign_id"]).any()
    assert daily.groupby("metric_date")["campaign_id"].nunique().eq(
        len(small_config.campaigns)
    ).all()


def test_campaign_daily_spend_remains_numeric_after_messiness(
    small_config,
):
    forced_defect_config = replace(
        small_config,
        defects=replace(
            small_config.defects,
            malformed_numeric_rate=1.0,
        ),
    )
    simulation = generate_historical_data(
        start_date=forced_defect_config.history_start_date,
        end_date=forced_defect_config.historical_cutoff_date,
        config=forced_defect_config,
    )
    raw_daily = apply_messiness(
        simulation.clean_tables,
        forced_defect_config,
    )["fact_campaign_daily"]

    numeric_spend = pd.to_numeric(raw_daily["spend"], errors="coerce")

    assert numeric_spend.notna().all()
    assert numeric_spend.ge(0).all()


def test_email_campaign_spend_uses_attempted_sends_and_production_cost(
    small_config,
):
    email_test_config = replace(
        small_config,
        history_start_date=date(2024, 1, 1),
        historical_cutoff_date=date(2024, 4, 1),
    )
    simulation = generate_historical_data(
        start_date=email_test_config.history_start_date,
        end_date=email_test_config.historical_cutoff_date,
        config=email_test_config,
    )

    campaign = next(
        campaign
        for campaign in email_test_config.campaigns
        if campaign.campaign_id == 103
    )
    daily = simulation.clean_tables["fact_campaign_daily"].copy()
    assignments = simulation.clean_tables[
        "fact_campaign_assignment"
    ].copy()

    send_date = date(2024, 4, 1)
    daily_dates = pd.to_datetime(daily["metric_date"]).dt.date
    assignment_dates = pd.to_datetime(
        assignments["assigned_at"]
    ).dt.date

    email_daily = daily[daily["campaign_id"] == 103]
    send_row = daily[
        (daily["campaign_id"] == 103)
        & (daily_dates == send_date)
    ].iloc[0]

    attempted_sends = int(
        (
            (assignments["campaign_id"] == 103)
            & (assignments["assignment_arm"] == "Treatment")
            & (assignment_dates == send_date)
        ).sum()
    )
    expected_spend = round(
        campaign.email_production_cost_per_send
        + attempted_sends * campaign.email_cost_per_attempted_send,
        2,
    )

    assert attempted_sends > 0
    assert send_row["spend"] == expected_spend
    assert email_daily.loc[
        daily_dates[email_daily.index] != send_date,
        "spend",
    ].eq(0).all()


def test_campaign_daily_state_hash_is_versioned_and_stable():
    record = {
        "metric_date": "2024-01-01",
        "campaign_id": 101,
        "spend": 100.0,
    }

    legacy_hash = _row_hash(record)
    versioned_hash = _state_hash(
        "fact_campaign_daily",
        record,
    )

    assert versioned_hash != legacy_hash
    assert versioned_hash == _state_hash(
        "fact_campaign_daily",
        record,
    )
    assert _state_hash("fact_payment", record) == legacy_hash


def test_versioned_hash_replays_campaign_daily_history_once(
    generation_service,
    small_config,
):
    generation_service.generate_through(
        small_config.historical_cutoff_date
    )

    baseline = generate_historical_data(
        start_date=small_config.history_start_date,
        end_date=small_config.historical_cutoff_date,
        config=small_config,
    )
    campaign_daily = baseline.clean_tables[
        "fact_campaign_daily"
    ]

    legacy_updates = []
    for record in campaign_daily.to_dict(orient="records"):
        normalized = _canonical_record(record)
        business_key = _canonical_json(
            [
                normalized["metric_date"],
                normalized["campaign_id"],
            ]
        )
        legacy_updates.append(
            (
                _row_hash(normalized),
                business_key,
            )
        )

    with generation_service.store.connection() as connection:
        connection.executemany(
            """
            UPDATE source_state
            SET row_hash = ?
            WHERE table_name = 'fact_campaign_daily'
              AND business_key = ?
            """,
            legacy_updates,
        )
        connection.commit()

    replay = generation_service.generate_through("2025-12-13")

    replay_day_count = (
        date(2025, 12, 13)
        - small_config.history_start_date
    ).days + 1
    expected_replayed_rows = (
        replay_day_count * len(small_config.campaigns)
    )

    assert replay["table_counts"]["fact_campaign_daily"][
        "changed_clean_rows"
    ] == expected_replayed_rows
    assert replay["table_counts"]["fact_campaign_daily"][
        "delivered_raw_rows"
    ] >= expected_replayed_rows

    following_day = generation_service.generate_through(
        "2025-12-14"
    )

    assert following_day["table_counts"]["fact_campaign_daily"][
        "changed_clean_rows"
    ] == len(small_config.campaigns)