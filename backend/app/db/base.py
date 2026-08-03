"""Core database engine, declarative base, and short-lived session scope."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from sqlalchemy import MetaData, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from shared.safe_paths import PROJECT_ROOT, ensure_runtime_directories, resolve_sqlite_path

CORE_DATABASE_NAME: Final[str] = "core.sqlite3"
NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    url: str
    path: Path | None
    echo: bool = False


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def database_settings(
    *,
    env: dict[str, str] | None = None,
    default_name: str = CORE_DATABASE_NAME,
) -> DatabaseSettings:
    """Load a constrained SQLite URL or an explicitly configured MySQL URL."""

    values = os.environ if env is None else env
    ensure_runtime_directories()
    configured_url = values.get("DATABASE_URL", "").strip()
    echo = values.get("SQL_ECHO", "").lower() == "true"
    if configured_url:
        if not configured_url.startswith(("mysql+asyncmy://", "mysql+aiomysql://")):
            raise ValueError("DATABASE_URL must use an approved asynchronous MySQL dialect")
        return DatabaseSettings(url=configured_url, path=None, echo=echo)
    raw_path = values.get("CORE_DATABASE_PATH", str(PROJECT_ROOT / "data" / default_name))
    path = resolve_sqlite_path(raw_path)
    return DatabaseSettings(url=_sqlite_url(path), path=path, echo=echo)


def _configure_sqlite_connection(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()


def create_engine_and_session_factory(
    settings: DatabaseSettings,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    options: dict[str, Any] = {
        "echo": settings.echo,
        "pool_pre_ping": True,
    }
    if settings.url.startswith("sqlite+"):
        options["connect_args"] = {"check_same_thread": False}
    engine = create_async_engine(settings.url, **options)
    if settings.url.startswith("sqlite+"):
        event.listen(engine.sync_engine, "connect", _configure_sqlite_connection)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    return engine, factory


@asynccontextmanager
async def session_scope(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """Yield one transaction-scoped session and never retain business objects."""

    async with factory() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise
        else:
            await session.commit()


async def dispose_engine(engine: AsyncEngine) -> None:
    await engine.dispose()