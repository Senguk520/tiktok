"""Collector-only jobs, normalized results, attempts, limits, and image registry."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from collector_app.db.base import CollectorBase


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


class CollectorJob(CollectorBase):
    __tablename__ = "collector_jobs"
    __table_args__ = (
        Index("ix_collector_job_due", "status", "next_attempt_at", "lease_until"),
        CheckConstraint(
            "source_mode IN ('OFFICIAL_API','PUBLIC_PAGE')", name="collector_source_mode"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="QUEUED", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class CollectorResult(CollectorBase):
    __tablename__ = "collector_results"
    __table_args__ = (
        UniqueConstraint("collector_job_id", name="uq_collector_result_job"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    collector_job_id: Mapped[str] = mapped_column(
        ForeignKey("collector_jobs.id", ondelete="CASCADE"), nullable=False
    )
    source_product_id: Mapped[str | None] = mapped_column(String(128))
    normalized_product: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    field_sources: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    unmapped_warnings: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CollectorAttempt(CollectorBase):
    __tablename__ = "collector_attempts"
    __table_args__ = (
        UniqueConstraint("collector_job_id", "attempt_number", name="uq_collector_attempt_number"),
        Index("ix_collector_attempt_started", "collector_job_id", "started_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    collector_job_id: Mapped[str] = mapped_column(
        ForeignKey("collector_jobs.id", ondelete="CASCADE"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(32))
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_redacted: Mapped[str | None] = mapped_column(Text)


class SourceRateLimit(CollectorBase):
    __tablename__ = "source_rate_limits"
    __table_args__ = (
        UniqueConstraint("source", "mode", "window_started_at", name="uq_source_limit_window"),
        CheckConstraint("window_seconds > 0", name="source_positive_window"),
        CheckConstraint("used >= 0", name="source_nonnegative_used"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    limit_value: Mapped[int] = mapped_column(Integer, nullable=False)
    used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ImageRecord(CollectorBase):
    __tablename__ = "image_records"
    __table_args__ = (
        UniqueConstraint("relative_path", name="uq_image_relative_path"),
        UniqueConstraint("sha256", name="uq_image_sha256"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    collector_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("collector_jobs.id", ondelete="SET NULL"), index=True
    )
    relative_path: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    source_url_redacted: Mapped[str | None] = mapped_column(Text)
    ready: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourceCredential(CollectorBase):
    __tablename__ = "source_credentials"
    __table_args__ = (
        UniqueConstraint("source", "credential_kind", name="uq_source_credential_kind"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    credential_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    key_version: Mapped[str] = mapped_column(String(16), nullable=False)
    aad_context: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )