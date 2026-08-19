from __future__ import annotations

import pytest

from app.settings import Settings


def test_railway_requires_a_volume(monkeypatch):
    monkeypatch.delenv("SQLITE_PATH", raising=False)
    monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH", raising=False)
    monkeypatch.setenv("RAILWAY_SERVICE_ID", "service-test")

    with pytest.raises(RuntimeError, match="persistent volume"):
        Settings.from_env()


def test_explicit_sqlite_path_takes_precedence(monkeypatch, tmp_path):
    database_path = tmp_path / "explicit.db"
    monkeypatch.setenv("SQLITE_PATH", str(database_path))
    monkeypatch.setenv("RAILWAY_SERVICE_ID", "service-test")

    assert Settings.from_env().database_path == database_path.resolve()
