"""Atomic Core schedule leases, guarded runs, and deauthorization stop."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ScheduleJob, ScheduleRun
from app.repositories.audit import AuditValue, sanitize_audit_details

_ALLOWED_RUN_STATES = frozenset(
    {"RUNNING", "SUCCEEDED", "FAILED", "BLOCKED", "LEASE_EXPIRED"}
)
_SAFE_FAILURE_SUMMARY = "scheduled operation failed"


class ScheduleLeaseLost(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ClaimedScheduleJob:
    id: str
    run_id: str
    shop_binding_id: str
    job_type: str
    schedule_kind: str
    interval_seconds: int | None
    run_at: datetime | None
    scheduled_for: datetime
    payload: dict[str, Any]
    required_scopes: tuple[str, ...]
    required_listing_mode: str | None
    quota_cost: int


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def claim_due_schedule_jobs(
    session: AsyncSession,
    *,
    worker_id: str,
    limit: int = 10,
    lease_seconds: int = 90,
    now: datetime | None = None,
) -> tuple[ClaimedScheduleJob, ...]:
    selected_worker = worker_id.strip()
    if not selected_worker or len(selected_worker) > 128 or limit <= 0 or lease_seconds <= 0:
        raise ValueError("valid worker, limit and lease are required")
    current = datetime.now(UTC) if now is None else _utc(now)
    due = and_(
        ScheduleJob.enabled.is_(True),
        ScheduleJob.next_run_at <= current,
        or_(ScheduleJob.lease_until.is_(None), ScheduleJob.lease_until <= current),
    )
    candidates = tuple(
        await session.scalars(
            select(ScheduleJob.id)
            .where(due)
            .order_by(ScheduleJob.next_run_at, ScheduleJob.id)
            .limit(limit)
        )
    )
    claimed: list[ClaimedScheduleJob] = []
    for job_id in candidates:
        result = await session.execute(
            update(ScheduleJob)
            .where(ScheduleJob.id == job_id, due)
            .values(
                lease_owner=selected_worker,
                lease_until=current + timedelta(seconds=lease_seconds),
                updated_at=current,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            continue
        await session.execute(
            update(ScheduleRun)
            .where(
                ScheduleRun.schedule_job_id == job_id,
                ScheduleRun.finished_at.is_(None),
            )
            .values(
                state="LEASE_EXPIRED",
                finished_at=current,
                error_code="schedule_lease_expired",
                error_redacted=_SAFE_FAILURE_SUMMARY,
            )
            .execution_options(synchronize_session=False)
        )
        job = await session.get(ScheduleJob, job_id, populate_existing=True)
        if job is None:
            continue
        run = ScheduleRun(
            schedule_job_id=job.id,
            state="RUNNING",
            worker_id=selected_worker,
            started_at=current,
            summary={},
        )
        session.add(run)
        await session.flush()
        claimed.append(
            ClaimedScheduleJob(
                id=job.id,
                run_id=run.id,
                shop_binding_id=job.shop_binding_id,
                job_type=job.job_type,
                schedule_kind=job.schedule_kind,
                interval_seconds=job.interval_seconds,
                run_at=job.run_at,
                scheduled_for=_utc(job.next_run_at),
                payload=dict(job.payload),
                required_scopes=tuple(job.required_scopes),
                required_listing_mode=job.required_listing_mode,
                quota_cost=job.quota_cost,
            )
        )
    return tuple(claimed)


async def assert_schedule_lease(
    session: AsyncSession,
    claim: ClaimedScheduleJob,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> None:
    current = datetime.now(UTC) if now is None else _utc(now)
    owned = await session.scalar(
        select(ScheduleJob.id)
        .join(ScheduleRun, ScheduleRun.schedule_job_id == ScheduleJob.id)
        .where(
            ScheduleJob.id == claim.id,
            ScheduleJob.enabled.is_(True),
            ScheduleJob.lease_owner == worker_id,
            ScheduleJob.lease_until > current,
            ScheduleRun.id == claim.run_id,
            ScheduleRun.worker_id == worker_id,
            ScheduleRun.state == "RUNNING",
            ScheduleRun.finished_at.is_(None),
        )
    )
    if owned is None:
        raise ScheduleLeaseLost("schedule lease is no longer owned by this run")


async def renew_schedule_lease(
    session: AsyncSession,
    claim: ClaimedScheduleJob,
    *,
    worker_id: str,
    lease_seconds: int = 90,
    now: datetime | None = None,
) -> None:
    current = datetime.now(UTC) if now is None else _utc(now)
    live_run = exists(
        select(ScheduleRun.id).where(
            ScheduleRun.id == claim.run_id,
            ScheduleRun.schedule_job_id == claim.id,
            ScheduleRun.worker_id == worker_id,
            ScheduleRun.state == "RUNNING",
            ScheduleRun.finished_at.is_(None),
        )
    )
    result = await session.execute(
        update(ScheduleJob)
        .where(
            ScheduleJob.id == claim.id,
            ScheduleJob.enabled.is_(True),
            ScheduleJob.lease_owner == worker_id,
            ScheduleJob.lease_until > current,
            live_run,
        )
        .values(
            lease_until=current + timedelta(seconds=lease_seconds),
            updated_at=current,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise ScheduleLeaseLost("schedule lease is no longer owned by this run")


async def finish_schedule_run(
    session: AsyncSession,
    claim: ClaimedScheduleJob,
    *,
    worker_id: str,
    state: str,
    next_run_at: datetime,
    enabled: bool,
    error_code: str | None = None,
    summary: dict[str, AuditValue] | None = None,
    now: datetime | None = None,
) -> None:
    normalized_state = state.strip().upper()
    if normalized_state not in _ALLOWED_RUN_STATES - {"RUNNING", "LEASE_EXPIRED"}:
        raise ValueError("schedule terminal state is invalid")
    if (normalized_state == "SUCCEEDED") != (error_code is None):
        raise ValueError("successful runs cannot have errors and failed runs require an error code")
    if error_code is not None and (
        not error_code or len(error_code) > 64 or not error_code.replace("_", "").isalnum()
    ):
        raise ValueError("schedule error code is invalid")
    current = datetime.now(UTC) if now is None else _utc(now)
    live_run = exists(
        select(ScheduleRun.id).where(
            ScheduleRun.id == claim.run_id,
            ScheduleRun.schedule_job_id == claim.id,
            ScheduleRun.worker_id == worker_id,
            ScheduleRun.state == "RUNNING",
            ScheduleRun.finished_at.is_(None),
        )
    )
    result = await session.execute(
        update(ScheduleJob)
        .where(
            ScheduleJob.id == claim.id,
            ScheduleJob.lease_owner == worker_id,
            ScheduleJob.lease_until > current,
            live_run,
        )
        .values(
            enabled=enabled,
            next_run_at=_utc(next_run_at),
            lease_owner=None,
            lease_until=None,
            updated_at=current,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise ScheduleLeaseLost("schedule lease was lost before completion")
    run_result = await session.execute(
        update(ScheduleRun)
        .where(
            ScheduleRun.id == claim.run_id,
            ScheduleRun.schedule_job_id == claim.id,
            ScheduleRun.worker_id == worker_id,
            ScheduleRun.state == "RUNNING",
            ScheduleRun.finished_at.is_(None),
        )
        .values(
            state=normalized_state,
            finished_at=current,
            summary=sanitize_audit_details(summary),
            error_code=error_code,
            error_redacted=None if error_code is None else _SAFE_FAILURE_SUMMARY,
        )
        .execution_options(synchronize_session=False)
    )
    if run_result.rowcount != 1:
        raise ScheduleLeaseLost("schedule run was closed concurrently")


async def set_schedule_enabled(
    session: AsyncSession,
    *,
    schedule_job_id: str,
    shop_binding_id: str,
    enabled: bool,
    next_run_at: datetime | None = None,
    now: datetime | None = None,
) -> bool:
    current = datetime.now(UTC) if now is None else _utc(now)
    values: dict[str, Any] = {
        "enabled": enabled,
        "lease_owner": None,
        "lease_until": None,
        "updated_at": current,
    }
    if enabled:
        values["next_run_at"] = current if next_run_at is None else _utc(next_run_at)
    result = await session.execute(
        update(ScheduleJob)
        .where(
            ScheduleJob.id == schedule_job_id,
            ScheduleJob.shop_binding_id == shop_binding_id,
            ScheduleJob.enabled.is_(not enabled),
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 1 and not enabled:
        await session.execute(
            update(ScheduleRun)
            .where(
                ScheduleRun.schedule_job_id == schedule_job_id,
                ScheduleRun.finished_at.is_(None),
            )
            .values(
                state="BLOCKED",
                finished_at=current,
                error_code="schedule_disabled",
                error_redacted=_SAFE_FAILURE_SUMMARY,
            )
            .execution_options(synchronize_session=False)
        )
    return result.rowcount == 1


async def disable_shop_jobs(session: AsyncSession, shop_binding_id: str) -> int:
    current = datetime.now(UTC)
    job_ids = tuple(
        await session.scalars(
            select(ScheduleJob.id).where(
                ScheduleJob.shop_binding_id == shop_binding_id,
                ScheduleJob.enabled.is_(True),
            )
        )
    )
    if not job_ids:
        return 0
    result = await session.execute(
        update(ScheduleJob)
        .where(ScheduleJob.id.in_(job_ids), ScheduleJob.enabled.is_(True))
        .values(enabled=False, lease_owner=None, lease_until=None, updated_at=current)
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(ScheduleRun)
        .where(
            ScheduleRun.schedule_job_id.in_(job_ids),
            ScheduleRun.finished_at.is_(None),
        )
        .values(
            state="BLOCKED",
            finished_at=current,
            error_code="shop_deauthorized",
            error_redacted=_SAFE_FAILURE_SUMMARY,
        )
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)
