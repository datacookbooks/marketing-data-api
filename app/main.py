"""FastAPI routes for incremental access to the generated source data."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
import logging
import secrets

from fastapi import FastAPI, Header, HTTPException, Query

from generator.historical_seed import TABLE_ORDER

from .db import SQLiteStore
from .generation_service import GenerationService
from .settings import Settings


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    service: GenerationService | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    service = service or GenerationService(SQLiteStore(settings.database_path))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        service.initialize_database()
        if settings.auto_backfill_on_startup:
            logger.info("Checking generated data through the current UTC date.")
            results = service.initialize_and_backfill()
            logger.info("Startup generation results: %s", results)
        yield

    application = FastAPI(
        title="Guitar Lessons Marketing Data API",
        description="Incremental messy source data for a marketing analytics project.",
        version="1.0.0",
        lifespan=lifespan,
    )

    @application.get("/")
    def root() -> dict:
        return {
            "name": "Guitar Lessons Marketing Data API",
            "version": "1.0.0",
            "docs": "/docs",
            "endpoints": {
                "health": "/health",
                "tables": "/tables",
                "raw_rows": "/tables/{table_name}/raw?since=0",
                "status": "/status",
            },
        }

    @application.get("/health")
    def health() -> dict:
        try:
            return service.store.health()
        except Exception as exc:
            logger.exception("Health check failed")
            raise HTTPException(status_code=503, detail="Database is unavailable.") from exc

    @application.get("/tables")
    def tables() -> dict:
        summary = service.store.table_summary(TABLE_ORDER)
        return {
            "count": len(summary),
            "tables": [
                {
                    **item,
                    "endpoint": f"/tables/{item['table_name']}/raw",
                }
                for item in summary
            ],
        }

    @application.get("/tables/{table_name}/raw")
    def raw_rows(
        table_name: str,
        since: int = Query(0, ge=0),
        limit: int | None = Query(None, ge=1),
    ) -> dict:
        if table_name not in TABLE_ORDER:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown table. Choose one of: {', '.join(TABLE_ORDER)}",
            )
        page_size = limit or settings.default_page_size
        if page_size > settings.max_page_size:
            raise HTTPException(
                status_code=422,
                detail=f"limit must be at most {settings.max_page_size}",
            )
        data, has_more = service.store.raw_page(table_name, since, page_size)
        next_since = data[-1]["_raw_row_id"] if data else since
        return {
            "table_name": table_name,
            "since": since,
            "next_since": next_since,
            "has_more": has_more,
            "count": len(data),
            "data": data,
        }

    @application.get("/status")
    def status() -> dict:
        return service.store.status(TABLE_ORDER)

    @application.post("/api/admin/generate")
    def generate(
        through: date | None = Query(None),
        authorization: str | None = Header(None),
    ) -> dict:
        expected = settings.daily_generation_token
        if not expected:
            raise HTTPException(
                status_code=503, detail="DAILY_GENERATION_TOKEN is not configured."
            )
        supplied = authorization or ""
        expected_header = f"Bearer {expected}"
        if not secrets.compare_digest(supplied, expected_header):
            raise HTTPException(status_code=401, detail="Unauthorized.")
        try:
            return service.generate_through(
                through or datetime.now(timezone.utc).date()
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Daily generation failed")
            raise HTTPException(status_code=500, detail="Daily generation failed.") from exc

    return application


app = create_app()
