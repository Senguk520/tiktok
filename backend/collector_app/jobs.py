"""Collector-owned collection job creation and safe status projections."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from collector_app.db.models import CollectorAttempt, CollectorJob, CollectorResult
from collector_app.sources.intents import SourceIdentity, normalize_source_identity


@dataclass(frozen=True, slots=True)
class CollectionJobCreated:
    job_id: str
    source: str
    source_mode: str
    status: str
    reused: bool


@dataclass(frozen=True, slots=True)
class CollectionJobSnapshot:
    job_id: str
    source: str
    source_mode: str
    status: str
    attempts: int
    max_attempts: int
    result_id: str | None
    imported: bool
    error_code: str | None


async def create_collection_job(
    session: AsyncSession,
    *,
    source: str,
    source_mode: str,
    source_url: str,
    now: datetime | None = None,
) -> CollectionJobCreated:
    identity = normalize_source_identity(
        source=source,
        mode=source_mode,
        source_url=source_url,
    )
    request_hash = _request_hash(identity)
    job_id = str(uuid4())
    current = datetime.now(UTC) if now is None else now
    statement = (
        sqlite_insert(CollectorJob)
        .values(
            id=job_id,
            source=identity.source,
            source_mode=identity.mode.value,
            source_url=identity.canonical_url,
            request_payload={},
            request_hash=request_hash,
            status="QUEUED",
            attempts=0,
            max_attempts=3,
            next_attempt_at=current,
            created_at=current,
            updated_at=current,
        )
        .on_conflict_do_nothing(index_elements=[CollectorJob.request_hash])
    )
    inserted = await session.execute(statement)
    job = await session.scalar(
        select(CollectorJob).where(CollectorJob.request_hash == request_hash)
    )
    if job is None:
        raise RuntimeError("collector job persistence failed")
    return CollectionJobCreated(
        job_id=job.id,
        source=job.source,
        source_mode=job.source_mode,
        status=job.status,
        reused=inserted.rowcount != 1,
    )


async def get_collection_job(
    session: AsyncSession,
    *,
    job_id: str,
) -> CollectionJobSnapshot:
    job = await session.get(CollectorJob, job_id)
    if job is None:
        raise LookupError("collector job was not found")
    result = await session.scalar(
        select(CollectorResult).where(CollectorResult.collector_job_id == job.id)
    )
    error_code = await session.scalar(
        select(CollectorAttempt.error_code)
        .where(CollectorAttempt.collector_job_id == job.id)
        .order_by(CollectorAttempt.attempt_number.desc())
        .limit(1)
    )
    return CollectionJobSnapshot(
        job_id=job.id,
        source=job.source,
        source_mode=job.source_mode,
        status=job.status,
        attempts=job.attempts,
        max_attempts=job.max_attempts,
        result_id=result.id if result is not None else None,
        imported=result is not None and result.imported_at is not None,
        error_code=error_code,
    )


def _request_hash(identity: SourceIdentity) -> str:
    canonical = json.dumps(
        {
            "source": identity.source,
            "source_mode": identity.mode.value,
            "source_product_id": identity.source_product_id,
            "source_url": identity.canonical_url,
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


__all__ = [
    "CollectionJobCreated",
    "CollectionJobSnapshot",
    "create_collection_job",
    "get_collection_job",
]