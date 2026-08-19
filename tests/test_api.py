from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings


def _client(generation_service, tmp_path) -> TestClient:
    settings = Settings(
        database_path=tmp_path / "test_marketing_data.db",
        daily_generation_token="test-token",
        auto_backfill_on_startup=False,
        default_page_size=100,
        max_page_size=1_000,
    )
    return TestClient(create_app(settings=settings, service=generation_service))


def test_incremental_table_endpoint(generation_service, small_config, tmp_path):
    generation_service.generate_through(small_config.historical_cutoff_date)
    with _client(generation_service, tmp_path) as client:
        response = client.get("/tables/dim_customer/raw", params={"since": 0, "limit": 2})

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert body["next_since"] > 0
    assert body["data"][0]["_raw_row_id"] < body["data"][1]["_raw_row_id"]


def test_admin_endpoint_requires_token(generation_service, tmp_path):
    with _client(generation_service, tmp_path) as client:
        response = client.post("/api/admin/generate")
    assert response.status_code == 401


def test_admin_endpoint_is_idempotent(generation_service, small_config, tmp_path):
    headers = {"Authorization": "Bearer test-token"}
    with _client(generation_service, tmp_path) as client:
        first = client.post(
            "/api/admin/generate",
            params={"through": small_config.historical_cutoff_date.isoformat()},
            headers=headers,
        )
        second = client.post(
            "/api/admin/generate",
            params={"through": small_config.historical_cutoff_date.isoformat()},
            headers=headers,
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "already_current"

