from __future__ import annotations

from datetime import date

import pytest

from app.db import SQLiteStore
from app.generation_service import GenerationService
from generator.config import ProjectConfig


@pytest.fixture
def small_config() -> ProjectConfig:
    return ProjectConfig(
        history_start_date=date(2025, 12, 1),
        historical_cutoff_date=date(2025, 12, 12),
        random_seed=20260818,
    )


@pytest.fixture
def generation_service(tmp_path, small_config) -> GenerationService:
    service = GenerationService(
        SQLiteStore(tmp_path / "test_marketing_data.db"),
        config=small_config,
    )
    service.initialize_database()
    return service

