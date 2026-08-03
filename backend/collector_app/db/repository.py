"""Transactional Collector job leases; results are import facts, never response cache."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from collector_app.db.models import CollectorAttempt, CollectorJob


async def claim_due_jobs(
    session: AsyncSession,
    *,
    worker_id: str,
    limit: int = 5,
    lease_seconds: int = 90,
    now: datetime | None = None,
) -> tuple[CollectorJob, ...]:
    if not worker_id or limit <= 0 or lease_seconds <= 0:
        raise ValueError("valid worker, limit and lease are required")
    current = datetime.now(UTC) if now is None else now
    due = and_(
        CollectorJob.status.in_(("QUEUED", "RETRY")),
        CollectorJob.attempts < CollectorJob.max_attempts,
        CollectorJob.next_attempt_at <= current,
        or_(CollectorJob.lease_until.is_(None), CollectorJob.lease_until <= current),
    )
    candidates = tuple(
        await session.scalars(
            select(CollectorJob.id)
            .where(due)
            .order_by(CollectorJob.next_attempt_at, CollectorJob.id)
            .limit(limit)
        )
    )
    claimed: list[str] = []
    lease_until = current + timedelta(seconds=lease_seconds)
    for job_id in candidates:
        result = await session.execute(
            update(CollectorJob)
            .where(CollectorJob.id == job_id, due)
            .values(
                status="RUNNING",
                lease_owner=worker_id,
                lease_until=lease_until,
                attempts=CollectorJob.attempts + 1,
                updated_at=current,
            )
        )
        if result.rowcount == 1:
            claimed.append(job_id)
    if not claimed:
        return ()
    rows = await session.scalars(
        select(CollectorJob)
        .where(CollectorJob.id.in_(claimed))
        .order_by(CollectorJob.next_attempt_at, CollectorJob.id)
    )
    return tuple(rows)


async def start_attempt(session: AsyncSession, job: CollectorJob) -> CollectorAttempt:
    attempt = CollectorAttempt(
        collector_job_id=job.id,
        attempt_number=job.attempts,
    )
    session.add(attempt)
    await session.flush()
    return attempt


async def finish_job(
    session: AsyncSession,
    job_id: str,
    *,
    worker_id: str,
    success: bool,
    retry_at: datetime | None = None,
) -> bool:
    target = "SUCCEEDED" if success else ("RETRY" if retry_at is not None else "FAILED")
    result = await session.execute(
        update(CollectorJob)
        .where(CollectorJob.id == job_id, CollectorJob.lease_owner == worker_id)
        .values(
            status=target,
            next_attempt_at=retry_at or datetime.now(UTC),
            lease_owner=None,
            lease_until=None,
            updated_at=datetime.now(UTC),
        )
    )
    return result.rowcount == 1