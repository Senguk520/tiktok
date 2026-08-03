from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.base import DatabaseSettings, create_engine_and_session_factory
from app.db.models import AuditLog, QuotaSnapshotModel, ScheduleJob, ScheduleRun, ShopBinding
from app.domain.enums import AuthorizationStatus, ListingMode, Scope
from app.domain.scopes import ScopeSet
from app.integrations.tiktok.oauth import TokenSet
from app.integrations.tiktok.products import ProductSubmission
from app.repositories.audit import record_audit_fact
from app.repositories.jobs import (
    ClaimedScheduleJob,
    ScheduleLeaseLost,
    claim_due_schedule_jobs,
    finish_schedule_run,
)
from app.use_cases.authorization import AuthorizedShop, bind_authorization, deauthorize_shop
from app.use_cases.products import DraftSubmissionPreparation, PreparedDraftSubmission
from app.use_cases.scheduler import (
    CoreScheduleDispatcher,
    ScheduleExecutionBlocked,
    ScheduleJobType,
    ScheduleKind,
    ScheduleWorker,
)
from migrations.core import migrate_engine
from shared.security import KeyRing, MasterKey


async def _factory(path: Path) -> tuple[Any, async_sessionmaker[AsyncSession]]:
    engine, factory = create_engine_and_session_factory(
        DatabaseSettings(
            url=f"sqlite+aiosqlite:///{path.as_posix()}",
            path=path,
        )
    )
    await migrate_engine(engine)
    return engine, factory


async def _seed_binding(
    factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    scopes: tuple[Scope, ...] = (Scope.PRODUCT_BASIC, Scope.PRODUCT_WRITE),
    listing_mode: ListingMode = ListingMode.LOCAL_REPLICATION,
) -> tuple[str, KeyRing]:
    master = MasterKey("v1", b"s" * 32)
    async with factory.begin() as session:
        binding = await bind_authorization(
            session,
            tokens=TokenSet(
                access_token="test-access-token",
                refresh_token="test-refresh-token",
                open_id="scheduler-owner",
                user_type=0,
                granted_scopes=ScopeSet(frozenset(scopes)),
                access_expires_at=now + timedelta(days=7),
                refresh_expires_at=now + timedelta(days=30),
            ),
            shops=(AuthorizedShop("scheduler-shop", "test-shop-cipher", "MY"),),
            key=master,
            expected_scopes=scopes,
        )
        binding.listing_mode = listing_mode.value
        await session.flush()
        binding_id = binding.id
    return binding_id, KeyRing.from_current(master)


async def _seed_due_job(
    factory: async_sessionmaker[AsyncSession],
    *,
    shop_binding_id: str,
    now: datetime,
    job_type: str = ScheduleJobType.SYNC_ORDERS.value,
    required_scopes: list[str] | None = None,
    required_listing_mode: str | None = None,
    quota_cost: int = 0,
    payload: dict[str, Any] | None = None,
) -> str:
    async with factory.begin() as session:
        job = ScheduleJob(
            shop_binding_id=shop_binding_id,
            job_type=job_type,
            schedule_kind=ScheduleKind.ONCE.value,
            run_at=now,
            next_run_at=now,
            enabled=True,
            payload=payload or {"window_seconds": 3600, "page_size": 20, "max_pages": 2},
            required_scopes=required_scopes or [],
            required_listing_mode=required_listing_mode,
            quota_cost=quota_cost,
        )
        session.add(job)
        await session.flush()
        return job.id


@pytest.mark.asyncio
async def test_schedule_claim_is_atomic_and_expired_owner_cannot_finish(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 3, 14, 8, 0, tzinfo=UTC)
    engine, factory = await _factory(tmp_path / "schedule-atomic.sqlite3")
    try:
        async with factory.begin() as session:
            shop = ShopBinding(
                open_id="atomic-owner",
                shop_id="atomic-shop",
                region="MY",
            )
            session.add(shop)
            await session.flush()
            shop_binding_id = shop.id
        job_id = await _seed_due_job(
            factory,
            shop_binding_id=shop_binding_id,
            now=now,
        )

        async def claim(worker_id: str) -> tuple[ClaimedScheduleJob, ...]:
            async with factory.begin() as session:
                return await claim_due_schedule_jobs(
                    session,
                    worker_id=worker_id,
                    lease_seconds=30,
                    now=now,
                )

        first, second = await asyncio.gather(claim("worker-a"), claim("worker-b"))
        claimed = first + second
        assert len(claimed) == 1
        original = claimed[0]
        original_worker = "worker-a" if first else "worker-b"
        assert original.id == job_id

        takeover_at = now + timedelta(seconds=31)
        async with factory.begin() as session:
            replacement = await claim_due_schedule_jobs(
                session,
                worker_id="worker-c",
                lease_seconds=30,
                now=takeover_at,
            )
        assert len(replacement) == 1
        assert replacement[0].run_id != original.run_id

        async with factory.begin() as session:
            with pytest.raises(ScheduleLeaseLost):
                await finish_schedule_run(
                    session,
                    original,
                    worker_id=original_worker,
                    state="FAILED",
                    next_run_at=takeover_at,
                    enabled=False,
                    error_code="stale_worker",
                    now=takeover_at + timedelta(seconds=1),
                )

        async with factory.begin() as session:
            runs = tuple(
                await session.scalars(
                    select(ScheduleRun).order_by(ScheduleRun.started_at, ScheduleRun.id)
                )
            )
            assert [run.state for run in runs] == ["LEASE_EXPIRED", "RUNNING"]
            assert runs[0].error_redacted == "scheduled operation failed"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_deauthorization_transactionally_disables_and_revokes_live_schedule(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 3, 14, 9, 0, tzinfo=UTC)
    engine, factory = await _factory(tmp_path / "schedule-deauthorize.sqlite3")
    try:
        binding_id, _key_ring = await _seed_binding(factory, now=now)
        job_id = await _seed_due_job(factory, shop_binding_id=binding_id, now=now)
        async with factory.begin() as session:
            claim = (
                await claim_due_schedule_jobs(
                    session,
                    worker_id="active-worker",
                    now=now,
                )
            )[0]
        async with factory.begin() as session:
            assert await deauthorize_shop(
                session,
                binding_id,
                now=now + timedelta(seconds=1),
            ) == 1

        async with factory.begin() as session:
            binding = await session.get(ShopBinding, binding_id)
            job = await session.get(ScheduleJob, job_id)
            run = await session.get(ScheduleRun, claim.run_id)
            assert binding is not None
            assert binding.authorization_status == AuthorizationStatus.DEAUTHORIZED.value
            assert binding.listing_mode == ListingMode.UNKNOWN.value
            assert job is not None and not job.enabled
            assert job.lease_owner is None and job.lease_until is None
            assert run is not None and run.state == "BLOCKED"
            assert run.error_code == "shop_deauthorized"
            with pytest.raises(ScheduleLeaseLost):
                await finish_schedule_run(
                    session,
                    claim,
                    worker_id="active-worker",
                    state="FAILED",
                    next_run_at=now,
                    enabled=False,
                    error_code="late_result",
                    now=now + timedelta(seconds=2),
                )
    finally:
        await engine.dispose()


class _NeverProductService:
    calls = 0

    async def prepare_draft_submission(self, *_args: Any, **_kwargs: Any) -> Any:
        self.calls += 1
        raise AssertionError("blocked dispatch must not reach product preparation")


class _NeverOrderService:
    async def sync_window(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("publication dispatch must not reach order synchronization")


def _claim_for(
    binding_id: str,
    now: datetime,
    *,
    required_scopes: tuple[str, ...] = (),
    required_listing_mode: str | None = None,
    quota_cost: int = 0,
) -> ClaimedScheduleJob:
    return ClaimedScheduleJob(
        id="11111111-1111-4111-8111-111111111111",
        run_id="22222222-2222-4222-8222-222222222222",
        shop_binding_id=binding_id,
        job_type=ScheduleJobType.PUBLISH_DRAFT.value,
        schedule_kind=ScheduleKind.ONCE.value,
        interval_seconds=None,
        run_at=now,
        scheduled_for=now,
        payload={"draft_id": "33333333-3333-4333-8333-333333333333"},
        required_scopes=required_scopes,
        required_listing_mode=required_listing_mode,
        quota_cost=quota_cost,
    )


@pytest.mark.asyncio
async def test_dispatcher_rechecks_mode_scope_quota_and_authorization_before_write(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 3, 14, 10, 0, tzinfo=UTC)
    engine, factory = await _factory(tmp_path / "schedule-guards.sqlite3")
    product_service = _NeverProductService()
    try:
        binding_id, key_ring = await _seed_binding(factory, now=now)
        dispatcher = CoreScheduleDispatcher(
            session_factory=factory,
            key_ring=key_ring,
            product_service=product_service,  # type: ignore[arg-type]
            order_service=_NeverOrderService(),  # type: ignore[arg-type]
            clock=lambda: now,
        )
        cases = (
            (
                _claim_for(
                    binding_id,
                    now,
                    required_listing_mode=ListingMode.GLOBAL_LEGACY.value,
                ),
                "schedule_listing_mode_changed",
            ),
            (
                _claim_for(
                    binding_id,
                    now,
                    required_scopes=(Scope.GLOBAL_PRODUCT_WRITE.value,),
                ),
                "schedule_scope_missing",
            ),
            (
                _claim_for(binding_id, now, quota_cost=1),
                "schedule_quota_block_unknown",
            ),
        )
        for claim, expected_code in cases:
            with pytest.raises(ScheduleExecutionBlocked) as caught:
                await dispatcher.execute(claim, worker_id="guard-worker")
            assert caught.value.code == expected_code

        async with factory.begin() as session:
            await deauthorize_shop(session, binding_id, now=now)
        with pytest.raises(ScheduleExecutionBlocked) as caught:
            await dispatcher.execute(_claim_for(binding_id, now), worker_id="guard-worker")
        assert caught.value.code == "schedule_shop_access_blocked"
        assert product_service.calls == 0
    finally:
        await engine.dispose()


class _BoundaryProductService:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self.events: list[str] = []
        self.idempotency_key: str | None = None

    async def prepare_draft_submission(
        self,
        session: AsyncSession,
        _context: Any,
        *,
        draft_id: str,
        idempotency_key: str,
    ) -> DraftSubmissionPreparation:
        assert session.in_transaction()
        self.events.append("prepare")
        self.idempotency_key = idempotency_key
        return DraftSubmissionPreparation(
            prepared=PreparedDraftSubmission(
                draft_id=draft_id,
                operation_id="44444444-4444-4444-8444-444444444444",
                product=None,  # type: ignore[arg-type]
                quota_snapshot_id=None,
            )
        )

    async def execute_draft_submission(
        self,
        context: Any,
        _prepared: PreparedDraftSubmission,
    ) -> ProductSubmission:
        self.events.append("execute")
        # This separate write transaction proves the lease/preparation
        # transaction was committed before any platform-like I/O begins.
        async with self._factory.begin() as session:
            await record_audit_fact(
                session,
                event_type="schedule.boundary",
                outcome="SUCCESS",
                shop_binding_id=context.shop_binding_id,
                details={"code": "platform_call_outside_transaction"},
            )
        return ProductSubmission(
            mode=context.listing_mode,
            product_id="scheduled-product-1",
            request_id="scheduled-request-1",
        )

    async def complete_draft_submission(
        self,
        session: AsyncSession,
        _context: Any,
        _prepared: PreparedDraftSubmission,
        _submission: ProductSubmission,
    ) -> object:
        assert session.in_transaction()
        self.events.append("complete")
        return object()


@pytest.mark.asyncio
async def test_schedule_worker_commits_claim_before_platform_call_and_finishes_once(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 3, 14, 11, 0, tzinfo=UTC)
    engine, factory = await _factory(tmp_path / "schedule-boundary.sqlite3")
    try:
        binding_id, key_ring = await _seed_binding(factory, now=now)
        async with factory.begin() as session:
            session.add(
                QuotaSnapshotModel(
                    shop_binding_id=binding_id,
                    region="MY",
                    stage="BEGINNER",
                    listing_limit=10,
                    locally_submitted_count=0,
                    confirmed_at=now,
                    expires_at=now + timedelta(hours=1),
                )
            )
        job_id = await _seed_due_job(
            factory,
            shop_binding_id=binding_id,
            now=now,
            job_type=ScheduleJobType.PUBLISH_DRAFT.value,
            required_scopes=[Scope.PRODUCT_WRITE.value],
            required_listing_mode=ListingMode.LOCAL_REPLICATION.value,
            quota_cost=1,
            payload={"draft_id": "55555555-5555-4555-8555-555555555555"},
        )
        product_service = _BoundaryProductService(factory)
        dispatcher = CoreScheduleDispatcher(
            session_factory=factory,
            key_ring=key_ring,
            product_service=product_service,  # type: ignore[arg-type]
            order_service=_NeverOrderService(),  # type: ignore[arg-type]
            clock=lambda: now,
        )
        worker = ScheduleWorker(
            session_factory=factory,
            dispatcher=dispatcher,
            worker_id="boundary-worker",
            lease_seconds=30,
            clock=lambda: now,
        )
        outcomes = await worker.run_once()
        assert [(item.state, item.error_code) for item in outcomes] == [("SUCCEEDED", None)]
        assert product_service.events == ["prepare", "execute", "complete"]
        assert product_service.idempotency_key == f"schedule:{job_id}:{int(now.timestamp())}"

        async with factory() as session:
            job = await session.get(ScheduleJob, job_id)
            run = await session.scalar(
                select(ScheduleRun).where(ScheduleRun.schedule_job_id == job_id)
            )
            boundary = await session.scalar(
                select(AuditLog).where(AuditLog.event_type == "schedule.boundary")
            )
            assert job is not None and not job.enabled
            assert job.lease_owner is None and job.lease_until is None
            assert run is not None and run.state == "SUCCEEDED"
            assert run.summary == {
                "code": "schedule_product_submitted",
                "job_type": ScheduleJobType.PUBLISH_DRAFT.value,
            }
            assert boundary is not None
    finally:
        await engine.dispose()
