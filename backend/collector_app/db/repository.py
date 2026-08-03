"""Transactional Collector leases and normalized result persistence.

Network and filesystem work must happen outside these functions. Each terminal
write uses the lease owner and attempt number as a compare-and-set guard, so a
stale worker cannot overwrite a reclaimed job.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from collector_app.db.models import (
    CollectorAttempt,
    CollectorJob,
    CollectorResult,
    ImageRecord,
)
from collector_app.images import StoredImage
from collector_app.normalizers import SourceProduct
from shared.collector_contract import (
    CONTRACT_VERSION,
    CollectorImageV1,
    CollectorProductV1,
    CollectorSkuV1,
)
from shared.redaction import redact_url

_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_FAILURE_SUMMARY = "collector operation failed"


class CollectorLeaseLost(RuntimeError):
    pass


class CollectorResultConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PersistedCollection:
    result_id: str
    duplicate_images: tuple[StoredImage, ...]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _lease_is_live(job: CollectorJob, *, worker_id: str, now: datetime) -> bool:
    return bool(
        job.status == "RUNNING"
        and job.lease_owner == worker_id
        and job.lease_until is not None
        and _aware(job.lease_until) > _aware(now)
    )


async def claim_due_jobs(
    session: AsyncSession,
    *,
    worker_id: str,
    limit: int = 5,
    lease_seconds: int = 90,
    now: datetime | None = None,
) -> tuple[CollectorJob, ...]:
    if not worker_id.strip() or len(worker_id) > 128 or limit <= 0 or lease_seconds <= 0:
        raise ValueError("valid worker, limit and lease are required")
    current = datetime.now(UTC) if now is None else now
    expired_lease = and_(
        CollectorJob.status == "RUNNING",
        or_(CollectorJob.lease_until.is_(None), CollectorJob.lease_until <= current),
    )
    exhausted = and_(expired_lease, CollectorJob.attempts >= CollectorJob.max_attempts)
    exhausted_ids = tuple(await session.scalars(select(CollectorJob.id).where(exhausted)))
    for job_id in exhausted_ids:
        result = await session.execute(
            update(CollectorJob)
            .where(CollectorJob.id == job_id, exhausted)
            .values(
                status="FAILED",
                next_attempt_at=current,
                lease_owner=None,
                lease_until=None,
                updated_at=current,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            await session.execute(
                update(CollectorAttempt)
                .where(
                    CollectorAttempt.collector_job_id == job_id,
                    CollectorAttempt.finished_at.is_(None),
                )
                .values(
                    finished_at=current,
                    outcome="FAILED",
                    error_code="collector_lease_expired",
                    error_redacted=_SAFE_FAILURE_SUMMARY,
                )
                .execution_options(synchronize_session=False)
            )
    queued_or_retryable = and_(
        CollectorJob.status.in_(("QUEUED", "RETRY")),
        CollectorJob.next_attempt_at <= current,
        or_(CollectorJob.lease_until.is_(None), CollectorJob.lease_until <= current),
    )
    expired_running = expired_lease
    due = and_(
        CollectorJob.attempts < CollectorJob.max_attempts,
        or_(queued_or_retryable, expired_running),
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
                lease_owner=worker_id.strip(),
                lease_until=lease_until,
                attempts=CollectorJob.attempts + 1,
                updated_at=current,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            await session.execute(
                update(CollectorAttempt)
                .where(
                    CollectorAttempt.collector_job_id == job_id,
                    CollectorAttempt.finished_at.is_(None),
                )
                .values(
                    finished_at=current,
                    outcome="LEASE_EXPIRED",
                    error_code="collector_lease_expired",
                    error_redacted=_SAFE_FAILURE_SUMMARY,
                )
                .execution_options(synchronize_session=False)
            )
            claimed.append(job_id)
    if not claimed:
        return ()
    rows = await session.scalars(
        select(CollectorJob)
        .where(CollectorJob.id.in_(claimed))
        .order_by(CollectorJob.next_attempt_at, CollectorJob.id)
        .execution_options(populate_existing=True)
    )
    return tuple(rows)


async def start_attempt(
    session: AsyncSession,
    job: CollectorJob,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> CollectorAttempt:
    current = datetime.now(UTC) if now is None else now
    if not _lease_is_live(job, worker_id=worker_id, now=current):
        raise CollectorLeaseLost("collector lease is not owned by this worker")
    attempt = CollectorAttempt(
        collector_job_id=job.id,
        attempt_number=job.attempts,
        started_at=current,
    )
    session.add(attempt)
    await session.flush()
    return attempt


async def renew_lease(
    session: AsyncSession,
    *,
    job_id: str,
    worker_id: str,
    attempt_number: int,
    lease_seconds: int,
    now: datetime | None = None,
) -> datetime:
    if not worker_id.strip() or lease_seconds <= 0 or attempt_number <= 0:
        raise ValueError("valid lease renewal values are required")
    current = datetime.now(UTC) if now is None else now
    lease_until = current + timedelta(seconds=lease_seconds)
    result = await session.execute(
        update(CollectorJob)
        .where(
            CollectorJob.id == job_id,
            CollectorJob.status == "RUNNING",
            CollectorJob.lease_owner == worker_id,
            CollectorJob.lease_until > current,
            CollectorJob.attempts == attempt_number,
        )
        .values(lease_until=lease_until, updated_at=current)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise CollectorLeaseLost("collector lease could not be renewed")
    return lease_until


async def persist_success(
    session: AsyncSession,
    *,
    job: CollectorJob,
    worker_id: str,
    product: SourceProduct,
    source_product_id: str,
    stored_images: Sequence[tuple[str, str, StoredImage]],
    now: datetime | None = None,
) -> PersistedCollection:
    """Atomically close a live lease and save only validated normalized facts."""

    current = datetime.now(UTC) if now is None else now
    if len(stored_images) != len(product.images):
        raise CollectorResultConflict("stored images do not match the normalized product")
    closed = await _close_owned_job(
        session,
        job=job,
        worker_id=worker_id,
        status="SUCCEEDED",
        next_attempt_at=current,
        now=current,
    )
    if not closed:
        raise CollectorLeaseLost("collector lease expired before success could be persisted")
    attempt_closed = await _close_attempt(
        session,
        job=job,
        outcome="SUCCEEDED",
        error_code=None,
        now=current,
    )
    if not attempt_closed:
        raise CollectorLeaseLost("collector attempt is no longer active")

    existing_result = await session.scalar(
        select(CollectorResult).where(CollectorResult.collector_job_id == job.id)
    )
    if existing_result is not None:
        raise CollectorResultConflict("collector job already has a result")

    contract_images: list[CollectorImageV1] = []
    referenced_image_ids: set[str] = set()
    duplicate_images: list[StoredImage] = []
    for expected, (source_url, role, stored) in zip(product.images, stored_images, strict=True):
        if source_url != expected.source_url or role != expected.role:
            raise CollectorResultConflict("stored image identity changed")
        record, created = await _register_image(
            session,
            job_id=job.id,
            source_url=source_url,
            stored=stored,
        )
        if not created:
            duplicate_images.append(stored)
        if record.id in referenced_image_ids:
            continue
        if record.width is None or record.height is None:
            raise CollectorResultConflict("registered image has no validated dimensions")
        referenced_image_ids.add(record.id)
        contract_images.append(
            CollectorImageV1(
                image_record_id=record.id,
                role=role,
                sha256=record.sha256,
                content_type=record.content_type,
                byte_size=record.byte_size,
                width=record.width,
                height=record.height,
            )
        )

    contract = CollectorProductV1(
        title=product.title,
        description=product.description,
        category_id=product.category_id,
        skus=tuple(
            CollectorSkuV1(
                seller_sku=item.seller_sku,
                price=item.price,
                currency=item.currency,
                attributes=item.attributes,
            )
            for item in product.skus
        ),
        images=tuple(contract_images),
        attributes=product.attributes,
        source_trace=product.source_trace,
        unmapped_warnings=product.unmapped_warnings,
    )
    result = CollectorResult(
        collector_job_id=job.id,
        source_product_id=source_product_id,
        normalized_product=contract.to_mapping(),
        field_sources=dict(product.source_trace),
        unmapped_warnings=list(product.unmapped_warnings),
        observed_at=current,
    )
    session.add(result)
    await session.flush()
    return PersistedCollection(result_id=result.id, duplicate_images=tuple(duplicate_images))


async def persist_failure(
    session: AsyncSession,
    *,
    job: CollectorJob,
    worker_id: str,
    error_code: str,
    retryable: bool,
    now: datetime | None = None,
) -> str:
    """Close one attempt with a stable code and bounded retry schedule."""

    if not _ERROR_CODE.fullmatch(error_code):
        raise ValueError("collector error code must be stable snake_case")
    current = datetime.now(UTC) if now is None else now
    can_retry = retryable and job.attempts < job.max_attempts
    status = "RETRY" if can_retry else "FAILED"
    retry_at = _retry_at(job.id, job.attempts, current) if can_retry else current
    closed = await _close_owned_job(
        session,
        job=job,
        worker_id=worker_id,
        status=status,
        next_attempt_at=retry_at,
        now=current,
    )
    if not closed:
        raise CollectorLeaseLost("collector lease expired before failure could be persisted")
    attempt_closed = await _close_attempt(
        session,
        job=job,
        outcome=status,
        error_code=error_code,
        now=current,
    )
    if not attempt_closed:
        raise CollectorLeaseLost("collector attempt is no longer active")
    return status


async def mark_result_imported(
    session: AsyncSession,
    *,
    result_id: str,
    imported_at: datetime | None = None,
) -> bool:
    current = datetime.now(UTC) if imported_at is None else imported_at
    result = await session.execute(
        update(CollectorResult)
        .where(
            CollectorResult.id == result_id,
            CollectorResult.imported_at.is_(None),
        )
        .values(imported_at=current)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 1:
        return True
    existing = await session.get(CollectorResult, result_id)
    if existing is None:
        raise LookupError("collector result was not found")
    return False


async def _register_image(
    session: AsyncSession,
    *,
    job_id: str,
    source_url: str,
    stored: StoredImage,
) -> tuple[ImageRecord, bool]:
    existing = await session.scalar(select(ImageRecord).where(ImageRecord.sha256 == stored.sha256))
    if existing is not None:
        if not existing.ready or existing.deleted_at is not None:
            raise CollectorResultConflict("matching image record is unavailable")
        if (
            existing.content_type != stored.content_type
            or existing.byte_size != stored.byte_size
            or existing.width != stored.width
            or existing.height != stored.height
        ):
            raise CollectorResultConflict("matching image metadata is inconsistent")
        return existing, False
    record = ImageRecord(
        collector_job_id=job_id,
        relative_path=stored.relative_path,
        sha256=stored.sha256,
        content_type=stored.content_type,
        byte_size=stored.byte_size,
        width=stored.width,
        height=stored.height,
        source_url_redacted=redact_url(source_url),
        ready=True,
    )
    session.add(record)
    await session.flush()
    return record, True


async def _close_owned_job(
    session: AsyncSession,
    *,
    job: CollectorJob,
    worker_id: str,
    status: str,
    next_attempt_at: datetime,
    now: datetime,
) -> bool:
    result = await session.execute(
        update(CollectorJob)
        .where(
            CollectorJob.id == job.id,
            CollectorJob.status == "RUNNING",
            CollectorJob.lease_owner == worker_id,
            CollectorJob.lease_until > now,
            CollectorJob.attempts == job.attempts,
        )
        .values(
            status=status,
            next_attempt_at=next_attempt_at,
            lease_owner=None,
            lease_until=None,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


async def _close_attempt(
    session: AsyncSession,
    *,
    job: CollectorJob,
    outcome: str,
    error_code: str | None,
    now: datetime,
) -> bool:
    result = await session.execute(
        update(CollectorAttempt)
        .where(
            CollectorAttempt.collector_job_id == job.id,
            CollectorAttempt.attempt_number == job.attempts,
            CollectorAttempt.finished_at.is_(None),
        )
        .values(
            finished_at=now,
            outcome=outcome,
            error_code=error_code,
            error_redacted=None if error_code is None else _SAFE_FAILURE_SUMMARY,
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def _retry_at(job_id: str, attempt_number: int, now: datetime) -> datetime:
    exponent = max(0, min(attempt_number - 1, 6))
    base_seconds = min(300, 5 * (2**exponent))
    jitter_ceiling = min(30, max(1, base_seconds // 4))
    digest = hashlib.sha256(f"{job_id}:{attempt_number}".encode()).digest()
    jitter = digest[0] % (jitter_ceiling + 1)
    return now + timedelta(seconds=base_seconds + jitter)


__all__ = [
    "CONTRACT_VERSION",
    "CollectorLeaseLost",
    "CollectorResultConflict",
    "PersistedCollection",
    "claim_due_jobs",
    "mark_result_imported",
    "persist_failure",
    "persist_success",
    "renew_lease",
    "start_attempt",
]