from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.base import DatabaseSettings, create_engine_and_session_factory
from app.db.models import (
    AuditLog,
    EncryptedCredential,
    IdempotentOperation,
    QuotaSnapshotModel,
    ScheduleJob,
    ScheduleRun,
    ScopeSnapshot,
    ShopBinding,
)
from app.domain.enums import AuthorizationStatus, ListingMode, Scope, WriteState
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
    ScheduleCommand,
    ScheduleExecutionBlocked,
    ScheduleJobType,
    ScheduleKind,
    ScheduleValidationError,
    ScheduleWorker,
    _next_run,
    change_schedule_state,
    create_schedule_job,
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
            shops=(
                AuthorizedShop(
                    "scheduler-shop",
                    "test-shop-cipher",
                    "MY",
                    shop_status="ACTIVE",
                    kyc_status="VERIFIED",
                ),
            ),
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
async def test_dispatcher_rechecks_mode_scope_quota_and_operational_access_before_write(
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

        for field, blocked_value, restored_value in (
            (
                "authorization_status",
                AuthorizationStatus.DEAUTHORIZED.value,
                AuthorizationStatus.ACTIVE.value,
            ),
            ("shop_status", "INACTIVE", "ACTIVE"),
            ("kyc_status", "PENDING", "VERIFIED"),
        ):
            async with factory.begin() as session:
                binding = await session.get(ShopBinding, binding_id)
                assert binding is not None
                setattr(binding, field, blocked_value)
            with pytest.raises(ScheduleExecutionBlocked) as caught:
                await dispatcher.execute(
                    _claim_for(binding_id, now),
                    worker_id="guard-worker",
                )
            assert caught.value.code == "schedule_shop_access_blocked"
            async with factory.begin() as session:
                binding = await session.get(ShopBinding, binding_id)
                assert binding is not None
                setattr(binding, field, restored_value)

        async with factory.begin() as session:
            snapshot = await session.scalar(
                select(ScopeSnapshot)
                .where(ScopeSnapshot.shop_binding_id == binding_id)
                .order_by(ScopeSnapshot.captured_at.desc(), ScopeSnapshot.id.desc())
                .limit(1)
            )
            assert snapshot is not None
            snapshot.access_expires_at = now
        with pytest.raises(ScheduleExecutionBlocked) as caught:
            await dispatcher.execute(_claim_for(binding_id, now), worker_id="guard-worker")
        assert caught.value.code == "schedule_shop_access_blocked"

        async with factory.begin() as session:
            snapshot = await session.scalar(
                select(ScopeSnapshot)
                .where(ScopeSnapshot.shop_binding_id == binding_id)
                .order_by(ScopeSnapshot.captured_at.desc(), ScopeSnapshot.id.desc())
                .limit(1)
            )
            credential = await session.scalar(
                select(EncryptedCredential)
                .where(
                    EncryptedCredential.owner_kind == "authorization",
                    EncryptedCredential.credential_kind == "access_token",
                )
                .order_by(
                    EncryptedCredential.updated_at.desc(),
                    EncryptedCredential.id.desc(),
                )
                .limit(1)
            )
            assert snapshot is not None and credential is not None
            snapshot.access_expires_at = now + timedelta(hours=1)
            credential.active = False
        with pytest.raises(ScheduleExecutionBlocked) as caught:
            await dispatcher.execute(_claim_for(binding_id, now), worker_id="guard-worker")
        assert caught.value.code == "schedule_shop_access_blocked"
        assert product_service.calls == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_schedule_creation_uses_the_same_operational_shop_guard(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 3, 14, 10, 30, tzinfo=UTC)
    engine, factory = await _factory(tmp_path / "schedule-create-guards.sqlite3")
    command = ScheduleCommand(
        job_type=ScheduleJobType.SYNC_ORDERS,
        schedule_kind=ScheduleKind.ONCE,
        run_at=now,
        payload={"window_seconds": 3600, "page_size": 20, "max_pages": 2},
    )
    try:
        binding_id, _key_ring = await _seed_binding(
            factory,
            now=now,
            scopes=(Scope.ORDER_INFO,),
        )
        async with factory.begin() as session:
            job = await create_schedule_job(
                session,
                shop_binding_id=binding_id,
                command=command,
                now=now,
            )
            assert job.enabled
            job_id = job.id

        async with factory.begin() as session:
            assert await change_schedule_state(
                session,
                shop_binding_id=binding_id,
                schedule_job_id=job_id,
                enabled=False,
                now=now,
            )
            snapshot = await session.scalar(
                select(ScopeSnapshot)
                .where(ScopeSnapshot.shop_binding_id == binding_id)
                .order_by(ScopeSnapshot.captured_at.desc(), ScopeSnapshot.id.desc())
                .limit(1)
            )
            assert snapshot is not None
            snapshot.granted_scopes = []
            snapshot.missing_scopes = [Scope.ORDER_INFO.value]
        async with factory.begin() as session:
            with pytest.raises(ScheduleValidationError):
                await create_schedule_job(
                    session,
                    shop_binding_id=binding_id,
                    command=command,
                    now=now,
                )
            with pytest.raises(ScheduleValidationError):
                await change_schedule_state(
                    session,
                    shop_binding_id=binding_id,
                    schedule_job_id=job_id,
                    enabled=True,
                    now=now,
                )
        async with factory.begin() as session:
            snapshot = await session.scalar(
                select(ScopeSnapshot)
                .where(ScopeSnapshot.shop_binding_id == binding_id)
                .order_by(ScopeSnapshot.captured_at.desc(), ScopeSnapshot.id.desc())
                .limit(1)
            )
            assert snapshot is not None
            snapshot.granted_scopes = [Scope.ORDER_INFO.value]
            snapshot.missing_scopes = []

        async with factory.begin() as session:
            binding = await session.get(ShopBinding, binding_id)
            assert binding is not None
            binding.kyc_status = "PENDING"
        async with factory.begin() as session:
            with pytest.raises(ScheduleValidationError):
                await create_schedule_job(
                    session,
                    shop_binding_id=binding_id,
                    command=command,
                    now=now,
                )

        async with factory.begin() as session:
            binding = await session.get(ShopBinding, binding_id)
            assert binding is not None
            binding.kyc_status = "VERIFIED"
            snapshot = await session.scalar(
                select(ScopeSnapshot)
                .where(ScopeSnapshot.shop_binding_id == binding_id)
                .order_by(ScopeSnapshot.captured_at.desc(), ScopeSnapshot.id.desc())
                .limit(1)
            )
            assert snapshot is not None
            snapshot.access_expires_at = now
        async with factory.begin() as session:
            with pytest.raises(ScheduleValidationError):
                await create_schedule_job(
                    session,
                    shop_binding_id=binding_id,
                    command=command,
                    now=now,
                )
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


@dataclass(frozen=True, slots=True)
class _ReconciliationPrepared:
    draft_id: str
    operation_id: str
    product: Any = None
    quota_snapshot_id: str | None = None
    reconciliation_required: bool = True


class _ReconciliationProductService:
    def __init__(self, submission: ProductSubmission | None) -> None:
        self._submission = submission
        self.events: list[str] = []
        self.create_calls = 0
        self.operation_id = "66666666-6666-4666-8666-666666666666"

    async def prepare_draft_submission(
        self,
        _session: AsyncSession,
        _context: Any,
        *,
        draft_id: str,
        idempotency_key: str,
    ) -> DraftSubmissionPreparation:
        assert idempotency_key.startswith("schedule:")
        self.events.append("prepare")
        return DraftSubmissionPreparation(
            prepared=_ReconciliationPrepared(
                draft_id=draft_id,
                operation_id=self.operation_id,
            )  # type: ignore[arg-type]
        )

    async def reconcile_draft_submission(
        self,
        _context: Any,
        _prepared: Any,
    ) -> ProductSubmission | None:
        self.events.append("reconcile")
        return self._submission

    async def execute_draft_submission(self, *_args: Any, **_kwargs: Any) -> Any:
        self.create_calls += 1
        raise AssertionError("reconciliation-required work must never call create")

    async def require_manual_reconciliation(
        self,
        session: AsyncSession,
        prepared: Any,
        *,
        reason: str,
    ) -> None:
        self.events.append("manual_review")
        operation = await session.get(IdempotentOperation, prepared.operation_id)
        assert operation is not None
        assert operation.state == WriteState.SUBMITTED.value
        operation.state = WriteState.MANUAL_REVIEW.value
        operation.manual_review_reason = reason

    async def complete_draft_submission(
        self,
        session: AsyncSession,
        _context: Any,
        prepared: PreparedDraftSubmission,
        submission: ProductSubmission,
    ) -> object:
        assert session.in_transaction()
        self.events.append("complete")
        operation = await session.get(IdempotentOperation, prepared.operation_id)
        assert operation is not None
        operation.state = WriteState.AUDITING.value
        operation.result_reference = submission.product_id
        return object()


async def _seed_reconciliation_operation(
    factory: async_sessionmaker[AsyncSession],
    *,
    shop_binding_id: str,
    operation_id: str,
) -> None:
    async with factory.begin() as session:
        session.add(
            IdempotentOperation(
                id=operation_id,
                shop_binding_id=shop_binding_id,
                operation="CREATE",
                business_key="scheduled-reconciliation",
                payload_hash="a" * 64,
                idempotency_key_hash="b" * 64,
                state=WriteState.SUBMITTED.value,
            )
        )


async def _run_reconciliation_job(
    factory: async_sessionmaker[AsyncSession],
    *,
    binding_id: str,
    key_ring: KeyRing,
    product_service: _ReconciliationProductService,
    now: datetime,
) -> tuple[str, str | None]:
    await _seed_reconciliation_operation(
        factory,
        shop_binding_id=binding_id,
        operation_id=product_service.operation_id,
    )
    await _seed_due_job(
        factory,
        shop_binding_id=binding_id,
        now=now,
        job_type=ScheduleJobType.PUBLISH_DRAFT.value,
        required_scopes=[Scope.PRODUCT_WRITE.value],
        required_listing_mode=ListingMode.LOCAL_REPLICATION.value,
        payload={"draft_id": "77777777-7777-4777-8777-777777777777"},
    )
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
        worker_id="reconciliation-worker",
        lease_seconds=30,
        clock=lambda: now,
    )
    outcome = (await worker.run_once())[0]
    return outcome.state, outcome.error_code


@pytest.mark.asyncio
async def test_scheduler_reconciles_unique_remote_product_without_create(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 3, 14, 12, 0, tzinfo=UTC)
    engine, factory = await _factory(tmp_path / "schedule-reconcile-unique.sqlite3")
    try:
        binding_id, key_ring = await _seed_binding(factory, now=now)
        product_service = _ReconciliationProductService(
            ProductSubmission(
                mode=ListingMode.LOCAL_REPLICATION,
                product_id="reconciled-product",
                request_id=None,
            )
        )
        state, error_code = await _run_reconciliation_job(
            factory,
            binding_id=binding_id,
            key_ring=key_ring,
            product_service=product_service,
            now=now,
        )
        assert (state, error_code) == ("SUCCEEDED", None)
        assert product_service.events == ["prepare", "reconcile", "complete"]
        assert product_service.create_calls == 0
        async with factory() as session:
            operation = await session.get(
                IdempotentOperation,
                product_service.operation_id,
            )
            assert operation is not None
            assert operation.state == WriteState.AUDITING.value
            assert operation.result_reference == "reconciled-product"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_scheduler_persists_manual_review_when_reconciliation_is_not_unique(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 3, 14, 13, 0, tzinfo=UTC)
    engine, factory = await _factory(tmp_path / "schedule-reconcile-manual.sqlite3")
    try:
        binding_id, key_ring = await _seed_binding(factory, now=now)
        product_service = _ReconciliationProductService(None)
        state, error_code = await _run_reconciliation_job(
            factory,
            binding_id=binding_id,
            key_ring=key_ring,
            product_service=product_service,
            now=now,
        )
        assert (state, error_code) == ("BLOCKED", "schedule_product_manual_review")
        assert product_service.events == ["prepare", "reconcile", "manual_review"]
        assert product_service.create_calls == 0
        async with factory() as session:
            operation = await session.get(
                IdempotentOperation,
                product_service.operation_id,
            )
            assert operation is not None
            assert operation.state == WriteState.MANUAL_REVIEW.value
            assert operation.manual_review_reason is not None
    finally:
        await engine.dispose()


def _interval_claim(
    scheduled_for: datetime,
    *,
    interval_seconds: int,
) -> ClaimedScheduleJob:
    return replace(
        _claim_for("88888888-8888-4888-8888-888888888888", scheduled_for),
        schedule_kind=ScheduleKind.INTERVAL.value,
        interval_seconds=interval_seconds,
        run_at=None,
    )


def test_next_interval_run_catches_up_after_long_downtime_in_one_calculation() -> None:
    scheduled_for = datetime(2000, 1, 1, tzinfo=UTC)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    next_run, enabled = _next_run(
        _interval_claim(scheduled_for, interval_seconds=60),
        now=now,
    )
    assert enabled
    assert next_run == now + timedelta(minutes=1)
    assert now < next_run <= now + timedelta(seconds=60)


@pytest.mark.parametrize(
    ("now", "expected"),
    (
        (
            datetime(2026, 3, 14, 11, 59, 59, tzinfo=UTC),
            datetime(2026, 3, 14, 12, 0, tzinfo=UTC),
        ),
        (
            datetime(2026, 3, 14, 12, 0, tzinfo=UTC),
            datetime(2026, 3, 14, 13, 0, tzinfo=UTC),
        ),
    ),
)
def test_next_interval_run_is_strictly_after_now_at_hour_boundaries(
    now: datetime,
    expected: datetime,
) -> None:
    claim = _interval_claim(
        datetime(2026, 3, 14, 8, 0, tzinfo=UTC),
        interval_seconds=3600,
    )
    next_run, enabled = _next_run(claim, now=now)
    assert enabled
    assert next_run == expected
    assert next_run > now
