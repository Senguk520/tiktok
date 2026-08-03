"""Versioned, repeatable Core schema migrations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db import models as _core_models  # noqa: F401
from app.db.base import Base, DatabaseSettings, create_engine_and_session_factory, database_settings

_VERSION_TABLE = "core_schema_migrations"
_V1_TABLE_NAMES = (
    "admin_sessions",
    "oauth_transactions",
    "encrypted_credentials",
    "shop_binding",
    "scope_snapshots",
    "listing_mode_evidence",
    "product_drafts",
    "product_links",
    "market_product_states",
    "idempotent_operations",
    "quota_snapshots",
    "order_sync_checkpoints",
    "schedule_jobs",
    "schedule_runs",
    "audit_logs",
    "rate_limit_windows",
)


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    apply: Callable[[AsyncConnection], Awaitable[None]]


async def _create_tables(connection: AsyncConnection, names: Sequence[str]) -> None:
    tables = [Base.metadata.tables[name] for name in names]
    await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))


async def _initial_schema(connection: AsyncConnection) -> None:
    await _create_tables(connection, _V1_TABLE_NAMES)


async def _product_image_assets(connection: AsyncConnection) -> None:
    await _create_tables(connection, ("product_image_assets",))


async def _normalized_order_records(connection: AsyncConnection) -> None:
    await _create_tables(connection, ("order_records", "order_line_records"))


async def _collector_import_receipts(connection: AsyncConnection) -> None:
    await _create_tables(connection, ("collector_import_receipts",))


MIGRATIONS: Sequence[Migration] = (
    Migration(version=1, name="initial_core_business_schema", apply=_initial_schema),
    Migration(version=2, name="product_image_asset_registry", apply=_product_image_assets),
    Migration(version=3, name="normalized_order_records", apply=_normalized_order_records),
    Migration(version=4, name="collector_import_receipts", apply=_collector_import_receipts),
)


async def _ensure_version_table(connection: AsyncConnection) -> None:
    await connection.exec_driver_sql(
        f"""
        CREATE TABLE IF NOT EXISTS {_VERSION_TABLE} (
            version INTEGER NOT NULL PRIMARY KEY,
            name VARCHAR(128) NOT NULL,
            applied_at VARCHAR(40) NOT NULL
        )
        """
    )


async def migrate_engine(engine: AsyncEngine) -> tuple[int, ...]:
    """Apply missing migrations transactionally and return applied versions."""

    applied_now: list[int] = []
    async with engine.begin() as connection:
        await _ensure_version_table(connection)
        result = await connection.execute(text(f"SELECT version FROM {_VERSION_TABLE}"))
        applied = {int(row[0]) for row in result}
        for migration in MIGRATIONS:
            if migration.version in applied:
                continue
            await migration.apply(connection)
            await connection.execute(
                text(
                    f"INSERT INTO {_VERSION_TABLE} (version, name, applied_at) "
                    "VALUES (:version, :name, :applied_at)"
                ),
                {
                    "version": migration.version,
                    "name": migration.name,
                    "applied_at": datetime.now(UTC).isoformat(),
                },
            )
            applied_now.append(migration.version)
    return tuple(applied_now)


async def migrate(settings: DatabaseSettings | None = None) -> tuple[int, ...]:
    selected = database_settings() if settings is None else settings
    engine, _factory = create_engine_and_session_factory(selected)
    try:
        return await migrate_engine(engine)
    finally:
        await engine.dispose()