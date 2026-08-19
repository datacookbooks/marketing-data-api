#!/usr/bin/env python3
"""Initialize SQLite, seed history, and catch up through today."""

from app.db import SQLiteStore
from app.generation_service import GenerationService
from app.settings import Settings


def main() -> None:
    settings = Settings.from_env()
    service = GenerationService(SQLiteStore(settings.database_path))
    for result in service.initialize_and_backfill():
        print(result)


if __name__ == "__main__":
    main()

