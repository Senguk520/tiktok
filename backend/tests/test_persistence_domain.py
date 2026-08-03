from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import DatabaseSettings, create_engine_and_session_factory
from app.db.models import ShopBinding
from app.domain.enums import ListingMode, OperationKind, Scope, WriteState
from app.domain.quota import QuotaDecision, QuotaSnapshot, decide_listing_quota
from app.domain.scopes import ScopeSet
from app.domain.state_machine import InvalidTransition, transition_write_state
from app.repositories.idempotency import (
    IdempotencyConflict,
    IdempotencyRequest,
    canonical_payload_hash,
    claim_due_operations,
    register_operation,
)
from app.repositories.rate_limits import consume_rate_limit
from collector_app.db.base import CollectorDatabaseSettings
from collector_app.db.base import create_engine_and_session_factory as collector_factory
from collector_app.db.models import CollectorJob
from collector_app.db.repository import claim_due_jobs
from migrations.collector import migrate_engine as migrate_collector
from migrations.core import migrate_engine as migrate_core


def test_scope_gap_and_write_state_fail_closed() -> None:
    scopes = ScopeSet.parse([Scope.PRODUCT_BASIC.value, "future.unknown.scope"])
    gap = scopes.gap([Scope.PRODUCT_BASIC, Scope.PRODUCT_WRITE])
    assert gap.missing == frozenset({Scope.PRODUCT_WRITE})
    assert transition_write_state(
        WriteState.SUBMITTED,
        WriteState.ACTIVE,
        operation=OperationKind.UPDATE_PRICE,
    ) is WriteState.ACTIVE
    with pytest.raises(InvalidTransition):
        transition_write_state(
            WriteState.SUBMITTED,
            WriteState.ACTIVE,
            operation=OperationKind.CREATE,
        )


def test_quota_is_unknown_or_stale_until_confirmed() -> None:
    now = datetime(2026, 8, 3, tzinfo=UTC)
    assert decide_listing_quota(None, 1, now=now) is QuotaDecision.BLOCK_UNKNOWN
    stale = QuotaSnapshot(10, 0, now - timedelta(days=2), now - timedelta(seconds=1))
    assert decide_listing_quota(stale, 1, now=now) is QuotaDecision.BLOCK_STALE
    current = QuotaSnapshot(10, 9, now, now + timedelta(hours=1))
    assert decide_listing_quota(current, 1, now=now) is QuotaDecision.ALLOW
    assert decide_listing_quota(current, 2, now=now) is QuotaDecision.QUEUE


@pytest.mark.asyncio
async def test_migrations_are_versioned_and_idempotent() -> None:
    core_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    collector_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        assert await migrate_core(core_engine) == (1,)
        assert await migrate_core(core_engine) == ()
        assert await migrate_collector(collector_engine) == (1,)
        assert await migrate_collector(collector_engine) == ()
        async with core_engine.connect() as connection:
            names = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
        assert {"shop_binding", "idempotent_operations", "core_schema_migrations"} <= names
        async with collector_engine.connect() as connection:
            names = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
        assert {"collector_jobs", "image_records", "collector_schema_migrations"} <= names
    finally:
        await core_engine.dispose()
        await collector_engine.dispose()


async def _core_factory() -> tuple[object, async_sessionmaker]:
    settings = DatabaseSettings(url="sqlite+aiosqlite:///:memory:", path=None)
    engine, factory = create_engine_and_session_factory(settings)
    await migrate_core(engine)
    return engine, factory


@pytest.mark.asyncio
async def test_idempotency_conflict_and_expired_lease_recovery() -> None:
    engine, factory = await _core_factory()
    now = datetime.now(UTC)
    try:
        async with factory() as session:
            shop = ShopBinding(
                open_id="owner-1",
                shop_id="shop-1",
                region="MY",
                listing_mode=ListingMode.UNKNOWN.value,
            )
            session.add(shop)
            await session.flush()
            payload_hash = canonical_payload_hash({"seller_sku": "SKU-1", "price": "12.30"})
            request = IdempotencyRequest(
                shop_binding_id=shop.id,
                operation="CREATE",
                business_key="SKU-1",
                payload_hash=payload_hash,
                idempotency_key="client-key-1",
            )
            operation, created = await register_operation(session, request)
            assert created
            operation.state = WriteState.QUEUED.value
            operation.lease_owner = "dead-worker"
            operation.lease_until = now - timedelta(seconds=1)
            await session.commit()

        async with factory() as session:
            claimed = await claim_due_operations(session, worker_id="worker-2", now=now)
            assert [item.business_key for item in claimed] == ["SKU-1"]
            assert claimed[0].attempts == 1
            await session.commit()

        async with factory() as session:
            existing, created = await register_operation(session, request)
            assert not created
            assert existing.business_key == "SKU-1"
            with pytest.raises(IdempotencyConflict):
                await register_operation(
                    session,
                    IdempotencyRequest(
                        shop_binding_id=request.shop_binding_id,
                        operation=request.operation,
                        business_key=request.business_key,
                        payload_hash=canonical_payload_hash({"seller_sku": "SKU-1", "price": "99"}),
                        idempotency_key="different-client-key",
                    ),
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rate_limit_window_is_persisted() -> None:
    engine, factory = await _core_factory()
    now = datetime(2026, 8, 3, 12, 0, 0, 100, tzinfo=UTC)
    try:
        async with factory() as session:
            args = {
                "app_key_hash": "a" * 64,
                "shop_id": "shop-1",
                "endpoint_key": "product.search",
                "operation_type": "READ",
                "limit_value": 2,
                "window_seconds": 60,
                "now": now,
            }
            assert (await consume_rate_limit(session, **args)).allowed
            assert (await consume_rate_limit(session, **args)).allowed
            decision = await consume_rate_limit(session, **args)
            assert not decision.allowed
            assert decision.retry_at == datetime(2026, 8, 3, 12, 1, tzinfo=UTC)
            await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_collector_job_lease_is_separate_and_recoverable() -> None:
    settings = CollectorDatabaseSettings(
        url="sqlite+aiosqlite:///:memory:",
        path=Path("collector.sqlite3"),
    )
    engine, factory = collector_factory(settings)
    await migrate_collector(engine)
    now = datetime.now(UTC)
    try:
        async with factory() as session:
            job = CollectorJob(
                source="CJ",
                source_mode="OFFICIAL_API",
                source_url="https://developers.cjdropshipping.com/api2.0/v1/product/query",
                request_hash="b" * 64,
                next_attempt_at=now - timedelta(seconds=1),
                lease_owner="dead",
                lease_until=now - timedelta(seconds=1),
            )
            session.add(job)
            await session.commit()
        async with factory() as session:
            claimed = await claim_due_jobs(session, worker_id="collector-2", now=now)
            assert len(claimed) == 1
            assert claimed[0].status == "RUNNING"
            await session.commit()
        async with factory() as session:
            stored = await session.scalar(select(CollectorJob))
            assert stored is not None and stored.attempts == 1
    finally:
        await engine.dispose()