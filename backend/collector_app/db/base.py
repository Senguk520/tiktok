"""Collector-only database engine and declarative metadata."""

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

COLLECTOR_DATABASE_NAME: Final[str] = "collector.sqlite3"
NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class CollectorBase(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


@dataclass(frozen=True, slots=True)
class CollectorDatabaseSettings:
    url: str
    path: Path
    echo: bool = False


def database_settings(
    *,
    env: dict[str, str] | None = None,
    default_name: str = COLLECTOR_DATABASE_NAME,
) -> CollectorDatabaseSettings:
    values = os.environ if env is None else env
    ensure_runtime_directories()
    raw_path = values.get("COLLECTOR_DATABASE_PATH", str(PROJECT_ROOT / "data" / default_name))
    path = resolve_sqlite_path(raw_path)
    return CollectorDatabaseSettings(
        url=f"sqlite+aiosqlite:///{path.as_posix()}",
        path=path,
        echo=values.get("SQL_ECHO", "").lower() == "true",
    )


def _configure_sqlite_connection(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()


def create_engine_and_session_factory(
    settings: CollectorDatabaseSettings,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        settings.url,
        echo=settings.echo,
        pool_pre_ping=True,
        connect_args={"check_same_thread": False},
    )
    event.listen(engine.sync_engine, "connect", _configure_sqlite_connection)
    return engine, async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def session_scope(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
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