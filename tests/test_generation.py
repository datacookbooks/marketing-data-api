from __future__ import annotations


def test_generation_is_idempotent(generation_service, small_config):
    first = generation_service.generate_through(small_config.historical_cutoff_date)
    assert first["status"] == "generated"
    assert first["delivered_raw_rows"] > 0

    before = generation_service.store.health()["raw_rows"]
    repeated = generation_service.generate_through(small_config.historical_cutoff_date)
    after = generation_service.store.health()["raw_rows"]

    assert repeated["status"] == "already_current"
    assert repeated["delivered_raw_rows"] == 0
    assert after == before


def test_next_date_appends_rows(generation_service, small_config):
    generation_service.generate_through(small_config.historical_cutoff_date)
    before = generation_service.store.health()["raw_rows"]

    result = generation_service.generate_through("2025-12-13")
    after = generation_service.store.health()["raw_rows"]

    assert result["status"] == "generated"
    assert result["changed_clean_rows"] > 0
    assert after > before

