"""Core FastAPI service entry point.

The browser-facing API is loopback-only, uses HttpOnly administrator sessions,
and delegates every commerce decision to application services. Run from the
repository root with ``uvicorn app.main:app --app-dir backend``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from uuid import uuid4

from fastapi import FastAPI, Request
from pydantic import BaseModel, ConfigDict

from app.api.auth import AdminAuthSettings
from app.api.auth import router as session_router
from app.api.errors import install_api_errors
from app.api.routes.audits import router as audits_router
from app.api.routes.orders import router as orders_router
from app.api.routes.products import router as products_router
from app.api.routes.schedules import router as schedules_router
from app.api.routes.shops import router as shops_router
from app.api.routes.tools import router as tools_router
from app.api.routes.webhooks import router as webhooks_router
from app.api.runtime import build_commerce_runtime
from app.db.base import (
    DatabaseSettings,
    create_engine_and_session_factory,
    database_settings,
    dispose_engine,
)
from app.use_cases.scheduler import (
    CoreScheduleDispatcher,
    ScheduleWorker,
    run_schedule_loop,
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
    runtime = build_commerce_runtime()
    application.state.commerce_runtime = runtime
    scheduler_dispatcher = CoreScheduleDispatcher(
        session_factory=session_factory,
        key_ring=runtime.key_ring,
        product_service=runtime.product_service,
        order_service=runtime.order_service,
    )
    scheduler_worker = ScheduleWorker(
        session_factory=session_factory,
        dispatcher=scheduler_dispatcher,
        worker_id=f"core-{uuid4()}",
    )
    scheduler_stop = asyncio.Event()
    scheduler_task = asyncio.create_task(run_schedule_loop(scheduler_worker, scheduler_stop))
    application.state.scheduler_worker = scheduler_worker
    try:
        yield
    finally:
        scheduler_stop.set()
        scheduler_task.cancel()
        with suppress(asyncio.CancelledError):
            await scheduler_task
        await dispose_engine(engine)
        application.state.database_settings = None
        application.state.db_engine = None
        application.state.db_session_factory = None
        application.state.admin_auth_settings = None
        application.state.commerce_runtime = None
        application.state.scheduler_worker = None


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
    application.include_router(shops_router)
    application.include_router(products_router)
    application.include_router(orders_router)
    application.include_router(tools_router)
    application.include_router(schedules_router)
    application.include_router(audits_router)
    application.include_router(webhooks_router)

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
