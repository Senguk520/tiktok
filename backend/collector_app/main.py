"""Collector FastAPI service with signed API and durable worker lifecycle.

Run with ``uvicorn collector_app.main:app --app-dir backend --port 8010``.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from pydantic import BaseModel, ConfigDict

from collector_app.api import install_collector_api
from collector_app.db.base import (
    CollectorDatabaseSettings,
    create_engine_and_session_factory,
    database_settings,
    dispose_engine,
)
from collector_app.images import default_image_downloader
from collector_app.sources import Alibaba1688OpenPlatformConfig, default_source_registry
from collector_app.worker import CollectorWorker, run_collector_loop
from migrations.collector import migrate_engine
from shared.http_security import install_security_middleware
from shared.safe_paths import PROJECT_ROOT
from shared.security import SecurityConfigurationError, load_internal_hmac_secret_from_env


class ServiceStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service: str
    status: str
    database: str


def _alibaba_1688_config(
    env: Mapping[str, str],
) -> Alibaba1688OpenPlatformConfig | None:
    values = (
        env.get("ALIBABA_1688_APP_KEY", "").strip(),
        env.get("ALIBABA_1688_APP_SECRET", "").strip(),
        env.get("ALIBABA_1688_ACCESS_TOKEN", "").strip(),
    )
    if not all(values):
        return None
    try:
        return Alibaba1688OpenPlatformConfig(
            app_key=values[0],
            app_secret=values[1],
            access_token=values[2],
        )
    except ValueError:
        return None


def create_app(*, start_worker: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        load_dotenv(PROJECT_ROOT / ".env", override=False)
        settings: CollectorDatabaseSettings = database_settings()
        engine, session_factory = create_engine_and_session_factory(settings)
        await migrate_engine(engine)
        try:
            internal_hmac_secret = load_internal_hmac_secret_from_env()
        except SecurityConfigurationError:
            internal_hmac_secret = None
        worker: CollectorWorker | None = None
        worker_stop: asyncio.Event | None = None
        worker_task: asyncio.Task[None] | None = None
        if start_worker:
            registry = default_source_registry(
                cj_access_token=os.environ.get("CJ_ACCESS_TOKEN"),
                alibaba_1688_config=_alibaba_1688_config(os.environ),
            )
            worker = CollectorWorker(
                session_factory=session_factory,
                registry=registry,
                images=default_image_downloader(),
                worker_id=f"collector-{uuid4()}",
            )
            worker_stop = asyncio.Event()
            worker_task = asyncio.create_task(run_collector_loop(worker, worker_stop))
        application.state.database_settings = settings
        application.state.db_engine = engine
        application.state.db_session_factory = session_factory
        application.state.internal_hmac_secret = internal_hmac_secret
        application.state.collector_worker = worker
        try:
            yield
        finally:
            if worker_stop is not None:
                worker_stop.set()
            if worker_task is not None:
                worker_task.cancel()
                with suppress(asyncio.CancelledError):
                    await worker_task
            await dispose_engine(engine)
            application.state.database_settings = None
            application.state.db_engine = None
            application.state.db_session_factory = None
            application.state.internal_hmac_secret = None
            application.state.collector_worker = None

    application = FastAPI(
        title="TikTok Single Shop Collector API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    install_security_middleware(application)
    install_collector_api(application)

    @application.get("/healthz", response_model=ServiceStatus, response_model_exclude_none=True)
    async def healthz(request: Request) -> ServiceStatus:
        settings = getattr(request.app.state, "database_settings", None)
        return ServiceStatus(
            service="collector",
            status="ok",
            database=str(settings.path.name) if settings is not None else "configured-at-startup",
        )

    @application.get("/", response_model=ServiceStatus)
    async def service_info(request: Request) -> ServiceStatus:
        return await healthz(request)

    return application


app = create_app()