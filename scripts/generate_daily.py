#!/usr/bin/env python3
"""Run the same idempotent generation path used by the protected API route."""

from datetime import datetime, timezone

from app.db import SQLiteStore
from app.generation_service import GenerationService
from app.settings import Settings


def main() -> None:
    settings = Settings.from_env()
    service = GenerationService(SQLiteStore(settings.database_path))
    service.initialize_database()
    print(service.generate_through(datetime.now(timezone.utc).date()))


if __name__ == "__main__":
    main()

