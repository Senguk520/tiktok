"""SQLite-backed schedules with guarded leases and transaction-free platform calls."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    ProductDraft,
    QuotaSnapshotModel,
    ScheduleJob,
    ScheduleRun,
)
from app.domain.enums import (
    ListingMode,
    ProductDraftStatus,
    Scope,
)
from app.domain.product_payload import normalized_product_from_payload
from app.domain.quota import QuotaDecision, QuotaSnapshot, decide_listing_quota
from app.repositories.audit import AuditValue, record_audit_fact
from app.repositories.jobs import (
    ClaimedScheduleJob,
    ScheduleLeaseLost,
    assert_schedule_lease,
    claim_due_schedule_jobs,
    finish_schedule_run,
    renew_schedule_lease,
    set_schedule_enabled,
)
from app.use_cases.commerce_context import CommerceAccessBlocked, ShopAccessContext
from app.use_cases.orders import OrderService
from app.use_cases.products import (
    DraftSubmissionPreparation,
    PreparedDraftSubmission,
    ProductService,
    ProductSubmissionBlocked,
    ProductSubmissionInProgress,
)
from app.use_cases.shop_access import (
    ShopAccessFactsBlocked,
    load_shop_access_context,
    require_operational_shop,
)
from shared.security import KeyRing

Clock = Callable[[], datetime]


class ScheduleKind(StrEnum):
    ONCE = "ONCE"
    INTERVAL = "INTERVAL"


class ScheduleJobType(StrEnum):
    PUBLISH_DRAFT = "PUBLISH_DRAFT"
    SYNC_ORDERS = "SYNC_ORDERS"


class ScheduleValidationError(ValueError):
    pass


class ScheduleExecutionBlocked(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ScheduleExecutionFailed(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ScheduleCommand:
    job_type: ScheduleJobType
    schedule_kind: ScheduleKind
    run_at: datetime
    payload: Mapping[str, Any]
    interval_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class ScheduleDispatchResult:
    summary: dict[str, AuditValue]


@dataclass(frozen=True, slots=True)
class ScheduleRunOutcome:
    schedule_job_id: str
    run_id: str
    state: str
    error_code: str | None = None


class ScheduleDispatcher(Protocol):
    async def execute(
        self,
        claim: ClaimedScheduleJob,
        *,
        worker_id: str,
    ) -> ScheduleDispatchResult: ...


def _utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None:
        raise ScheduleValidationError(f"{field} must include a timezone")
    return value.astimezone(UTC)


def _uuid(value: object, *, field: str) -> str:
    try:
        parsed = UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ScheduleValidationError(f"{field} must be a UUID") from exc
    return str(parsed)


def _validate_payload_keys(payload: Mapping[str, Any], allowed: frozenset[str]) -> None:
    if set(payload) != set(allowed):
        raise ScheduleValidationError("schedule payload fields do not match the job contract")


def _normalized_command(
    command: ScheduleCommand,
    *,
    now: datetime,
) -> tuple[datetime, dict[str, Any], tuple[str, ...], str | None, int]:
    run_at = _utc(command.run_at, field="run_at")
    if run_at < now - timedelta(seconds=1) or run_at > now + timedelta(days=366):
        raise ScheduleValidationError("schedule run time must be current or within 366 days")
    interval = command.interval_seconds
    if command.schedule_kind is ScheduleKind.ONCE:
        if interval is not None:
            raise ScheduleValidationError("one-time schedules cannot define an interval")
    elif interval is None or not 60 <= interval <= 31 * 24 * 60 * 60:
        raise ScheduleValidationError("interval must be between 60 seconds and 31 days")

    payload = dict(command.payload)
    if command.job_type is ScheduleJobType.PUBLISH_DRAFT:
        if command.schedule_kind is not ScheduleKind.ONCE:
            raise ScheduleValidationError("draft publication supports one-time schedules only")
        _validate_payload_keys(payload, frozenset({"draft_id"}))
        payload = {"draft_id": _uuid(payload["draft_id"], field="draft_id")}
        return run_at, payload, (Scope.PRODUCT_WRITE.value,), "CURRENT", 1

    if command.job_type is ScheduleJobType.SYNC_ORDERS:
        _validate_payload_keys(
            payload,
            frozenset({"window_seconds", "page_size", "max_pages"}),
        )
        try:
            window_seconds = int(payload["window_seconds"])
            page_size = int(payload["page_size"])
            max_pages = int(payload["max_pages"])
        except (TypeError, ValueError) as exc:
            raise ScheduleValidationError("order schedule payload must contain integers") from exc
        if not 60 <= window_seconds <= 7 * 24 * 60 * 60:
            raise ScheduleValidationError("order sync window must be between one minute and seven days")
        if not 1 <= page_size <= 100 or not 1 <= max_pages <= 100:
            raise ScheduleValidationError("order sync page limits are invalid")
        payload = {
            "window_seconds": window_seconds,
            "page_size": page_size,
            "max_pages": max_pages,
        }
        return run_at, payload, (Scope.ORDER_INFO.value,), None, 0

    raise ScheduleValidationError("schedule job type is not supported")


async def create_schedule_job(
    session: AsyncSession,
    *,
    shop_binding_id: str,
    command: ScheduleCommand,
    now: datetime | None = None,
) -> ScheduleJob:
    current = datetime.now(UTC) if now is None else _utc(now, field="now")
    run_at, payload, scopes, mode_marker, quota_cost = _normalized_command(
        command,
        now=current,
    )
    try:
        facts = await require_operational_shop(
            session,
            shop_binding_id=shop_binding_id,
            now=current,
        )
        binding = facts.binding
        if not set(scopes).issubset(set(facts.snapshot.granted_scopes)):
            raise ScheduleValidationError("shop scope requirements are not granted")
    except ShopAccessFactsBlocked as exc:
        raise ScheduleValidationError(str(exc)) from exc
    required_mode: str | None = None
    if mode_marker == "CURRENT":
        if binding.listing_mode == ListingMode.UNKNOWN.value:
            raise ScheduleValidationError("listing mode is not verified")
        required_mode = binding.listing_mode
        draft = await session.get(ProductDraft, payload["draft_id"])
        if (
            draft is None
            or draft.shop_binding_id != shop_binding_id
            or draft.status != ProductDraftStatus.READY.value
            or not draft.human_confirmed
            or not normalized_product_from_payload(draft.normalized_payload).ready_for_platform_submission
        ):
            raise ScheduleValidationError("draft is not ready for scheduled publication")
    job = ScheduleJob(
        shop_binding_id=shop_binding_id,
        job_type=command.job_type.value,
        schedule_kind=command.schedule_kind.value,
        interval_seconds=command.interval_seconds,
        run_at=run_at if command.schedule_kind is ScheduleKind.ONCE else None,
        next_run_at=run_at,
        enabled=True,
        payload=payload,
        required_scopes=list(scopes),
        required_listing_mode=required_mode,
        quota_cost=quota_cost,
    )
    session.add(job)
    await session.flush()
    return job


async def list_schedule_jobs(
    session: AsyncSession,
    *,
    shop_binding_id: str,
    limit: int = 100,
) -> tuple[ScheduleJob, ...]:
    if not 1 <= limit <= 200:
        raise ValueError("schedule page size must be between 1 and 200")
    rows = await session.scalars(
        select(ScheduleJob)
        .where(ScheduleJob.shop_binding_id == shop_binding_id)
        .order_by(ScheduleJob.created_at.desc(), ScheduleJob.id.desc())
        .limit(limit)
    )
    return tuple(rows)


async def list_schedule_runs(
    session: AsyncSession,
    *,
    shop_binding_id: str,
    schedule_job_id: str,
    limit: int = 100,
) -> tuple[ScheduleRun, ...]:
    if not 1 <= limit <= 200:
        raise ValueError("schedule run page size must be between 1 and 200")
    rows = await session.scalars(
        select(ScheduleRun)
        .join(ScheduleJob, ScheduleJob.id == ScheduleRun.schedule_job_id)
        .where(
            ScheduleRun.schedule_job_id == schedule_job_id,
            ScheduleJob.shop_binding_id == shop_binding_id,
        )
        .order_by(ScheduleRun.started_at.desc(), ScheduleRun.id.desc())
        .limit(limit)
    )
    return tuple(rows)


async def change_schedule_state(
    session: AsyncSession,
    *,
    shop_binding_id: str,
    schedule_job_id: str,
    enabled: bool,
    now: datetime | None = None,
) -> bool:
    current = datetime.now(UTC) if now is None else _utc(now, field="now")
    if enabled:
        job = await session.get(ScheduleJob, schedule_job_id)
        if job is None or job.shop_binding_id != shop_binding_id:
            return False
        try:
            facts = await require_operational_shop(
                session,
                shop_binding_id=shop_binding_id,
                now=current,
            )
        except ShopAccessFactsBlocked as exc:
            raise ScheduleValidationError(str(exc)) from exc
        if not set(job.required_scopes).issubset(set(facts.snapshot.granted_scopes)):
            raise ScheduleValidationError("shop scope requirements are not granted")
    return await set_schedule_enabled(
        session,
        schedule_job_id=schedule_job_id,
        shop_binding_id=shop_binding_id,
        enabled=enabled,
        now=current,
    )


class CoreScheduleDispatcher:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        key_ring: KeyRing | None,
        product_service: ProductService,
        order_service: OrderService,
        clock: Clock | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._key_ring = key_ring
        self._product_service = product_service
        self._order_service = order_service
        self._clock = clock or (lambda: datetime.now(UTC))

    async def execute(
        self,
        claim: ClaimedScheduleJob,
        *,
        worker_id: str,
    ) -> ScheduleDispatchResult:
        context = await self._load_context_and_check(claim)
        if claim.job_type == ScheduleJobType.PUBLISH_DRAFT.value:
            return await self._publish_draft(claim, context=context, worker_id=worker_id)
        if claim.job_type == ScheduleJobType.SYNC_ORDERS.value:
            return await self._sync_orders(claim, context=context)
        raise ScheduleExecutionBlocked("schedule_job_type_unsupported")

    async def _load_context_and_check(self, claim: ClaimedScheduleJob) -> ShopAccessContext:
        if self._key_ring is None:
            raise ScheduleExecutionBlocked("schedule_master_key_unavailable")
        now = self._clock()
        async with self._session_factory() as session:
            try:
                context = await load_shop_access_context(
                    session,
                    shop_binding_id=claim.shop_binding_id,
                    key_ring=self._key_ring,
                    now=now,
                )
            except CommerceAccessBlocked as exc:
                raise ScheduleExecutionBlocked("schedule_shop_access_blocked") from exc
            snapshot = await session.scalar(
                select(QuotaSnapshotModel)
                .where(QuotaSnapshotModel.shop_binding_id == claim.shop_binding_id)
                .order_by(QuotaSnapshotModel.confirmed_at.desc(), QuotaSnapshotModel.id.desc())
                .limit(1)
            )
            if claim.required_listing_mode is not None and (
                context.listing_mode.value != claim.required_listing_mode
            ):
                raise ScheduleExecutionBlocked("schedule_listing_mode_changed")
            granted = {scope.value for scope in context.scopes.values}
            if not set(claim.required_scopes).issubset(granted):
                raise ScheduleExecutionBlocked("schedule_scope_missing")
            if claim.quota_cost > 0:
                quota = (
                    None
                    if snapshot is None
                    else QuotaSnapshot(
                        listing_limit=snapshot.listing_limit,
                        submitted_count=snapshot.locally_submitted_count,
                        confirmed_at=_stored_utc(snapshot.confirmed_at),
                        expires_at=_stored_utc(snapshot.expires_at),
                        source=snapshot.source,
                    )
                )
                decision = decide_listing_quota(quota, claim.quota_cost, now=now)
                if decision is not QuotaDecision.ALLOW:
                    raise ScheduleExecutionBlocked(f"schedule_quota_{decision.value.lower()}")
        return context

    async def _publish_draft(
        self,
        claim: ClaimedScheduleJob,
        *,
        context: ShopAccessContext,
        worker_id: str,
    ) -> ScheduleDispatchResult:
        idempotency_key = f"schedule:{claim.id}:{int(claim.scheduled_for.timestamp())}"
        preparation: DraftSubmissionPreparation
        try:
            async with self._session_factory.begin() as session:
                await assert_schedule_lease(
                    session,
                    claim,
                    worker_id=worker_id,
                    now=self._clock(),
                )
                preparation = await self._product_service.prepare_draft_submission(
                    session,
                    context,
                    draft_id=str(claim.payload["draft_id"]),
                    idempotency_key=idempotency_key,
                )
        except (CommerceAccessBlocked, ProductSubmissionBlocked) as exc:
            raise ScheduleExecutionBlocked("schedule_product_precondition_blocked") from exc
        except ProductSubmissionInProgress as exc:
            raise ScheduleExecutionBlocked("schedule_product_requires_reconciliation") from exc
        if preparation.replayed is not None:
            return ScheduleDispatchResult(
                {"code": "schedule_product_replayed", "job_type": claim.job_type}
            )
        prepared = preparation.prepared
        if prepared is None:
            raise ScheduleExecutionFailed("schedule_product_preparation_invalid")
        if getattr(prepared, "reconciliation_required", False):
            try:
                submission = await self._product_service.reconcile_draft_submission(
                    context,
                    prepared,
                )
            except BaseException as exc:
                await self._require_product_manual_reconciliation(
                    claim,
                    prepared,
                    worker_id=worker_id,
                    reason=(
                        "TikTok reconciliation could not confirm a unique remote product"
                    ),
                )
                raise ScheduleExecutionBlocked("schedule_product_manual_review") from exc
            if submission is None:
                await self._require_product_manual_reconciliation(
                    claim,
                    prepared,
                    worker_id=worker_id,
                    reason=(
                        "TikTok create cannot be uniquely reconciled from a complete same-mode search"
                    ),
                )
                raise ScheduleExecutionBlocked("schedule_product_manual_review")
        else:
            try:
                submission = await self._product_service.execute_draft_submission(
                    context,
                    prepared,
                )
            except BaseException as exc:
                try:
                    async with self._session_factory.begin() as session:
                        await assert_schedule_lease(
                            session,
                            claim,
                            worker_id=worker_id,
                            now=self._clock(),
                        )
                        await self._product_service.fail_draft_submission(
                            session,
                            prepared,
                            exc,
                        )
                except ScheduleLeaseLost:
                    raise
                except Exception as persistence_exc:
                    raise ScheduleExecutionFailed(
                        "schedule_product_failure_persistence_failed"
                    ) from persistence_exc
                raise ScheduleExecutionFailed("schedule_product_submission_failed") from exc
        async with self._session_factory.begin() as session:
            await assert_schedule_lease(
                session,
                claim,
                worker_id=worker_id,
                now=self._clock(),
            )
            await self._product_service.complete_draft_submission(
                session,
                context,
                prepared,
                submission,
            )
        return ScheduleDispatchResult(
            {"code": "schedule_product_submitted", "job_type": claim.job_type}
        )

    async def _require_product_manual_reconciliation(
        self,
        claim: ClaimedScheduleJob,
        prepared: PreparedDraftSubmission,
        *,
        worker_id: str,
        reason: str,
    ) -> None:
        try:
            async with self._session_factory.begin() as session:
                await assert_schedule_lease(
                    session,
                    claim,
                    worker_id=worker_id,
                    now=self._clock(),
                )
                await self._product_service.require_manual_reconciliation(
                    session,
                    prepared,
                    reason=reason,
                )
        except ScheduleLeaseLost:
            raise
        except Exception as exc:
            raise ScheduleExecutionFailed(
                "schedule_product_manual_review_persistence_failed"
            ) from exc

    async def _sync_orders(
        self,
        claim: ClaimedScheduleJob,
        *,
        context: ShopAccessContext,
    ) -> ScheduleDispatchResult:
        now = self._clock()
        try:
            summary = await self._order_service.sync_window(
                self._session_factory,
                context,
                window_start=now - timedelta(seconds=int(claim.payload["window_seconds"])),
                window_end=now,
                page_size=int(claim.payload["page_size"]),
                max_pages=int(claim.payload["max_pages"]),
                stream_name=f"schedule.{claim.id}",
            )
        except Exception as exc:
            raise ScheduleExecutionFailed("schedule_order_sync_failed") from exc
        return ScheduleDispatchResult(
            {
                "code": "schedule_order_sync_completed",
                "job_type": claim.job_type,
                "page_count": summary.pages,
            }
        )


class ScheduleWorker:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        dispatcher: ScheduleDispatcher,
        worker_id: str,
        lease_seconds: int = 90,
        clock: Clock | None = None,
    ) -> None:
        selected_worker = worker_id.strip()
        if not selected_worker or len(selected_worker) > 128 or lease_seconds <= 0:
            raise ValueError("valid scheduler worker identity and lease are required")
        self._session_factory = session_factory
        self._dispatcher = dispatcher
        self._worker_id = selected_worker
        self._lease_seconds = lease_seconds
        self._clock = clock or (lambda: datetime.now(UTC))

    async def run_once(self, *, limit: int = 5) -> tuple[ScheduleRunOutcome, ...]:
        async with self._session_factory() as session:
            claims = await claim_due_schedule_jobs(
                session,
                worker_id=self._worker_id,
                limit=limit,
                lease_seconds=self._lease_seconds,
                now=self._clock(),
            )
            await session.commit()
        outcomes: list[ScheduleRunOutcome] = []
        for claim in claims:
            outcomes.append(await self._run_claim(claim))
        return tuple(outcomes)

    async def _run_claim(self, claim: ClaimedScheduleJob) -> ScheduleRunOutcome:
        try:
            await self._renew(claim)
            result = await self._dispatch_with_heartbeat(claim)
            await self._finish(claim, state="SUCCEEDED", summary=result.summary)
            return ScheduleRunOutcome(claim.id, claim.run_id, "SUCCEEDED")
        except ScheduleLeaseLost:
            return ScheduleRunOutcome(
                claim.id,
                claim.run_id,
                "LEASE_LOST",
                "schedule_lease_lost",
            )
        except ScheduleExecutionBlocked as exc:
            return await self._finish_failure(claim, state="BLOCKED", code=exc.code)
        except ScheduleExecutionFailed as exc:
            return await self._finish_failure(claim, state="FAILED", code=exc.code)
        except Exception:
            return await self._finish_failure(
                claim,
                state="FAILED",
                code="schedule_internal_error",
            )

    async def _dispatch_with_heartbeat(
        self,
        claim: ClaimedScheduleJob,
    ) -> ScheduleDispatchResult:
        task = asyncio.create_task(
            self._dispatcher.execute(claim, worker_id=self._worker_id)
        )
        heartbeat_seconds = max(1.0, self._lease_seconds / 3)
        try:
            while True:
                done, _pending = await asyncio.wait(
                    {task},
                    timeout=heartbeat_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if done:
                    return await task
                await self._renew(claim)
        except BaseException:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            raise

    async def _renew(self, claim: ClaimedScheduleJob) -> None:
        async with self._session_factory.begin() as session:
            await renew_schedule_lease(
                session,
                claim,
                worker_id=self._worker_id,
                lease_seconds=self._lease_seconds,
                now=self._clock(),
            )

    async def _finish_failure(
        self,
        claim: ClaimedScheduleJob,
        *,
        state: str,
        code: str,
    ) -> ScheduleRunOutcome:
        try:
            await self._finish(
                claim,
                state=state,
                error_code=code,
                summary={"code": code, "job_type": claim.job_type},
            )
        except ScheduleLeaseLost:
            return ScheduleRunOutcome(
                claim.id,
                claim.run_id,
                "LEASE_LOST",
                "schedule_lease_lost",
            )
        return ScheduleRunOutcome(claim.id, claim.run_id, state, code)

    async def _finish(
        self,
        claim: ClaimedScheduleJob,
        *,
        state: str,
        error_code: str | None = None,
        summary: dict[str, AuditValue] | None = None,
    ) -> None:
        now = self._clock()
        next_run_at, enabled = _next_run(claim, now=now)
        async with self._session_factory.begin() as session:
            await finish_schedule_run(
                session,
                claim,
                worker_id=self._worker_id,
                state=state,
                next_run_at=next_run_at,
                enabled=enabled,
                error_code=error_code,
                summary=summary,
                now=now,
            )
            await record_audit_fact(
                session,
                event_type=f"schedule.run.{state.lower()}",
                outcome="SUCCESS" if state == "SUCCEEDED" else state,
                shop_binding_id=claim.shop_binding_id,
                resource_type="schedule_job",
                resource_id=claim.id,
                details={
                    "code": "schedule_run_succeeded" if error_code is None else error_code,
                    "job_type": claim.job_type,
                    "run_state": state,
                },
                now=now,
            )


def _stored_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _next_run(claim: ClaimedScheduleJob, *, now: datetime) -> tuple[datetime, bool]:
    if claim.schedule_kind == ScheduleKind.ONCE.value:
        return now, False
    interval_seconds = claim.interval_seconds
    if (
        claim.schedule_kind != ScheduleKind.INTERVAL.value
        or interval_seconds is None
        or interval_seconds <= 0
    ):
        raise ScheduleExecutionBlocked("schedule_rule_invalid")
    interval = timedelta(seconds=interval_seconds)
    elapsed = now - claim.scheduled_for
    intervals_to_advance = max(1, elapsed // interval + 1)
    return claim.scheduled_for + intervals_to_advance * interval, True


async def run_schedule_loop(
    worker: ScheduleWorker,
    stop: asyncio.Event,
    *,
    idle_seconds: float = 1.0,
    error_seconds: float = 5.0,
) -> None:
    if idle_seconds <= 0 or error_seconds <= 0:
        raise ValueError("scheduler polling delays must be positive")
    while not stop.is_set():
        try:
            outcomes = await worker.run_once()
            delay = 0.0 if outcomes else idle_seconds
        except asyncio.CancelledError:
            raise
        except Exception:
            delay = error_seconds
        if delay > 0:
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass