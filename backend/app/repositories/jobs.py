"""Lease-based schedule job claiming and transactional deauthorization stop."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ScheduleJob


async def claim_due_schedule_jobs(
    session: AsyncSession,
    *,
    worker_id: str,
    limit: int = 10,
    lease_seconds: int = 60,
    now: datetime | None = None,
) -> tuple[ScheduleJob, ...]:
    if not worker_id or limit <= 0 or lease_seconds <= 0:
        raise ValueError("valid worker, limit and lease are required")
    current = datetime.now(UTC) if now is None else now
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
    claimed: list[str] = []
    for job_id in candidates:
        result = await session.execute(
            update(ScheduleJob)
            .where(ScheduleJob.id == job_id, due)
            .values(
                lease_owner=worker_id,
                lease_until=current + timedelta(seconds=lease_seconds),
                updated_at=current,
            )
        )
        if result.rowcount == 1:
            claimed.append(job_id)
    if not claimed:
        return ()
    rows = await session.scalars(
        select(ScheduleJob)
        .where(ScheduleJob.id.in_(claimed))
        .order_by(ScheduleJob.next_run_at, ScheduleJob.id)
    )
    return tuple(rows)


async def disable_shop_jobs(session: AsyncSession, shop_binding_id: str) -> int:
    result = await session.execute(
        update(ScheduleJob)
        .where(ScheduleJob.shop_binding_id == shop_binding_id, ScheduleJob.enabled.is_(True))
        .values(enabled=False, lease_owner=None, lease_until=None, updated_at=datetime.now(UTC))
    )
    return int(result.rowcount or 0)