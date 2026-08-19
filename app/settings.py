"""Environment-backed application settings."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _database_path() -> Path:
    explicit_path = os.environ.get("SQLITE_PATH")
    if explicit_path:
        return Path(explicit_path).expanduser().resolve()

    volume_path = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if volume_path:
        return Path(volume_path) / "marketing_data.db"

    railway_markers = (
        "RAILWAY_ENVIRONMENT_ID",
        "RAILWAY_PROJECT_ID",
        "RAILWAY_SERVICE_ID",
    )
    if any(os.environ.get(name) for name in railway_markers):
        raise RuntimeError(
            "This Railway service needs a persistent volume. "
            "Attach one at /data before starting the API."
        )

    return ROOT_DIR / "data" / "marketing_data.db"


@dataclass(frozen=True)
class Settings:
    database_path: Path
    daily_generation_token: str | None
    auto_backfill_on_startup: bool = True
    default_page_size: int = 1_000
    max_page_size: int = 10_000

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_path=_database_path(),
            daily_generation_token=os.environ.get("DAILY_GENERATION_TOKEN"),
            auto_backfill_on_startup=_as_bool(
                os.environ.get("AUTO_BACKFILL_ON_STARTUP"), True
            ),
            default_page_size=int(os.environ.get("API_DEFAULT_PAGE_SIZE", "1000")),
            max_page_size=int(os.environ.get("API_MAX_PAGE_SIZE", "10000")),
        )
