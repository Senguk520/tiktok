"""Core FastAPI service entry point.

The browser-facing API is loopback-only, uses HttpOnly administrator sessions,
and delegates every commerce decision to application services. Run from the
repository root with ``uvicorn app.main:app --app-dir backend``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from pydantic import BaseModel, ConfigDict

from app.api.auth import AdminAuthSettings
from app.api.auth import router as session_router
from app.api.errors import install_api_errors
from app.api.routes.orders import router as orders_router
from app.api.routes.products import router as products_router
from app.api.runtime import build_commerce_runtime
from app.db.base import (
    DatabaseSettings,
    create_engine_and_session_factory,
    database_settings,
    dispose_engine,
)
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
    application.state.admin_auth_settings = AdminAuthSettings.from_env()
    application.state.commerce_runtime = build_commerce_runtime()
    try:
        yield
    finally:
        await dispose_engine(engine)
        application.state.database_settings = None
        application.state.db_engine = None
        application.state.db_session_factory = None
        application.state.admin_auth_settings = None
        application.state.commerce_runtime = None


def create_app() -> FastAPI:
    application = FastAPI(
        title="TikTok Single Shop Core API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    install_security_middleware(application, allow_browser=True, loopback_only=True)
    install_api_errors(application)
    application.include_router(session_router)
    application.include_router(products_router)
    application.include_router(orders_router)

    @application.get("/healthz", response_model=ServiceStatus, response_model_exclude_none=True)
    async def healthz(request: Request) -> ServiceStatus:
        """Return process health without querying or exposing business data."""

        settings = getattr(request.app.state, "database_settings", None)
        return ServiceStatus(
            service="core",
            status="ok",
            database=(
                str(settings.path.name)
                if settings is not None and settings.path is not None
                else "configured-at-startup"
            ),
        )

    @application.get("/", response_model=ServiceStatus)
    async def service_info(request: Request) -> ServiceStatus:
        return await healthz(request)

    return application


app = create_app()
