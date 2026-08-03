"""Core FastAPI service entry point.

This foundation endpoint intentionally exposes only health and service metadata.
Business routers are added in later stages; secrets never cross this boundary.
Run from the repository root with ``uvicorn app.main:app --app-dir backend``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict

from app.db.base import DatabaseSettings, create_engine_and_session_factory, database_settings, dispose_engine
from migrations.core import migrate_engine
from shared.http_security import install_security_middleware


class ServiceStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service: str
    status: str
    database: str


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    settings: DatabaseSettings = database_settings()
    engine, session_factory = create_engine_and_session_factory(settings)
    await migrate_engine(engine)
    application.state.database_settings = settings
    application.state.db_engine = engine
    application.state.db_session_factory = session_factory
    try:
        yield
    finally:
        await dispose_engine(engine)
        application.state.db_engine = None
        application.state.db_session_factory = None


app = FastAPI(
    title="TikTok Single Shop Core API",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
install_security_middleware(app, allow_browser=True)


@app.get("/healthz", response_model=ServiceStatus, response_model_exclude_none=True)
async def healthz() -> ServiceStatus:
    """Return process health without querying or exposing business data."""

    settings = getattr(app.state, "database_settings", None)
    return ServiceStatus(
        service="core",
        status="ok",
        database=(
            str(settings.path.name)
            if settings is not None and settings.path is not None
            else "configured-at-startup"
        ),
    )


@app.get("/", response_model=ServiceStatus)
async def service_info() -> ServiceStatus:
    return await healthz()