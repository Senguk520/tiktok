"""Lease-aware Collector worker orchestration with short database transactions."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from collector_app.db.models import CollectorJob
from collector_app.db.repository import (
    CollectorLeaseLost,
    claim_due_jobs,
    persist_failure,
    persist_success,
    renew_lease,
    start_attempt,
)
from collector_app.images import ImageDownloader, StoredImage
from collector_app.normalizers import normalize_artifact
from collector_app.sources import SourceAdapterError, SourceMode, SourceRegistry, SourceRequest

Clock = Callable[[], datetime]
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    id: str
    source: str
    mode: SourceMode
    source_url: str
    payload: Mapping[str, Any]
    attempt_number: int


@dataclass(frozen=True, slots=True)
class JobRunOutcome:
    job_id: str
    status: str
    error_code: str | None = None


class CollectorWorker:
    """Run bounded batches; scheduling and process lifetime remain outside."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        registry: SourceRegistry,
        images: ImageDownloader,
        worker_id: str,
        clock: Clock | None = None,
        lease_seconds: int = 90,
    ) -> None:
        selected_worker = worker_id.strip()
        if not selected_worker or len(selected_worker) > 128 or lease_seconds <= 0:
            raise ValueError("valid worker identity and lease are required")
        self._session_factory = session_factory
        self._registry = registry
        self._images = images
        self._worker_id = selected_worker
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lease_seconds = lease_seconds

    async def run_once(self, *, limit: int = 5) -> tuple[JobRunOutcome, ...]:
        claimed = await self._claim(limit=limit)
        if not claimed:
            return ()
        return tuple(await asyncio.gather(*(self._run_claimed(job) for job in claimed)))

    async def _claim(self, *, limit: int) -> tuple[ClaimedJob, ...]:
        now = self._clock()
        async with self._session_factory() as session:
            jobs = await claim_due_jobs(
                session,
                worker_id=self._worker_id,
                limit=limit,
                lease_seconds=self._lease_seconds,
                now=now,
            )
            snapshots: list[ClaimedJob] = []
            for job in jobs:
                await start_attempt(
                    session,
                    job,
                    worker_id=self._worker_id,
                    now=now,
                )
                try:
                    mode = SourceMode(job.source_mode)
                except ValueError as exc:
                    raise ValueError("persisted collector source mode is invalid") from exc
                snapshots.append(
                    ClaimedJob(
                        id=job.id,
                        source=job.source,
                        mode=mode,
                        source_url=job.source_url,
                        payload=dict(job.request_payload),
                        attempt_number=job.attempts,
                    )
                )
            await session.commit()
        return tuple(snapshots)

    async def _run_claimed(self, job: ClaimedJob) -> JobRunOutcome:
        downloaded: list[StoredImage] = []
        try:
            request = SourceRequest(
                source=job.source,
                mode=job.mode,
                source_url=job.source_url,
                payload=job.payload,
            )
            adapter = self._registry.resolve(request)
            artifact = await adapter.collect(request)
            await self._renew(job)
            if artifact.source != job.source.strip().upper() or artifact.mode is not job.mode:
                raise SourceAdapterError(
                    "source_identity_mismatch",
                    "source artifact identity does not match its claimed job",
                )
            normalized = normalize_artifact(artifact)
            stored_with_identity: list[tuple[str, str, StoredImage]] = []
            for image in normalized.product.images:
                await self._renew(job)
                stored = await self._images.download(source=job.source, url=image.source_url)
                downloaded.append(stored)
                stored_with_identity.append((image.source_url, image.role, stored))
            await self._renew(job)
        except CollectorLeaseLost:
            await self._discard_all(downloaded)
            return JobRunOutcome(job.id, "LEASE_LOST", "collector_lease_lost")
        except SourceAdapterError as exc:
            await self._discard_all(downloaded)
            code = exc.code if _ERROR_CODE.fullmatch(exc.code) else "source_adapter_error"
            return await self._record_failure(job, code=code, retryable=exc.retryable)
        except (TypeError, ValueError):
            await self._discard_all(downloaded)
            return await self._record_failure(job, code="invalid_job_request", retryable=False)
        except Exception:
            # The raw exception is intentionally neither logged nor persisted here.
            await self._discard_all(downloaded)
            return await self._record_failure(job, code="worker_internal_error", retryable=False)

        try:
            async with self._session_factory() as session:
                current = await session.get(CollectorJob, job.id)
                if current is None:
                    raise CollectorLeaseLost("collector job no longer exists")
                persisted = await persist_success(
                    session,
                    job=current,
                    worker_id=self._worker_id,
                    product=normalized.product,
                    source_product_id=normalized.source_product_id,
                    stored_images=stored_with_identity,
                    now=self._clock(),
                )
                await session.commit()
        except CollectorLeaseLost:
            await self._discard_all(downloaded)
            return JobRunOutcome(job.id, "LEASE_LOST", "collector_lease_lost")
        except Exception:
            await self._discard_all(downloaded)
            return await self._record_failure(
                job,
                code="result_persistence_failed",
                retryable=True,
            )

        await self._discard_all(list(persisted.duplicate_images))
        return JobRunOutcome(job.id, "SUCCEEDED")

    async def _renew(self, job: ClaimedJob) -> None:
        async with self._session_factory() as session:
            await renew_lease(
                session,
                job_id=job.id,
                worker_id=self._worker_id,
                attempt_number=job.attempt_number,
                lease_seconds=self._lease_seconds,
                now=self._clock(),
            )
            await session.commit()

    async def _record_failure(
        self,
        job: ClaimedJob,
        *,
        code: str,
        retryable: bool,
    ) -> JobRunOutcome:
        try:
            async with self._session_factory() as session:
                current = await session.get(CollectorJob, job.id)
                if current is None:
                    raise CollectorLeaseLost("collector job no longer exists")
                status = await persist_failure(
                    session,
                    job=current,
                    worker_id=self._worker_id,
                    error_code=code,
                    retryable=retryable,
                    now=self._clock(),
                )
                await session.commit()
        except CollectorLeaseLost:
            return JobRunOutcome(job.id, "LEASE_LOST", "collector_lease_lost")
        except Exception:
            # Failure persistence is deliberately reported with a stable code;
            # the database or driver diagnostic must not escape the worker.
            return JobRunOutcome(job.id, "PERSISTENCE_ERROR", "failure_persistence_failed")
        return JobRunOutcome(job.id, status, code)

    async def _discard_all(self, images: list[StoredImage]) -> None:
        for image in images:
            try:
                await self._images.discard(image)
            except Exception:
                # Cleanup is best effort and must never replace the primary
                # outcome with path, driver, or operating-system diagnostics.
                continue