"""Core business facts persisted in the Core SQLite database.

Secrets are represented only as authenticated ciphertext. Remote payloads and
buyer PII are deliberately absent; JSON columns hold normalized business
facts, redacted diagnostics, or operator-confirmed metadata.
"""

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

from app.db.base import Base
from app.domain.enums import AuthorizationStatus, ListingMode, WriteState


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class AdminSession(Base):
    __tablename__ = "admin_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    csrf_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    client_fingerprint_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OAuthTransaction(Base):
    __tablename__ = "oauth_transactions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    state_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expected_account: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="PENDING", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))


class EncryptedCredential(Base, TimestampMixin):
    __tablename__ = "encrypted_credentials"
    __table_args__ = (
        Index("ix_credential_owner_kind", "owner_kind", "owner_id", "credential_kind"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    credential_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    key_version: Mapped[str] = mapped_column(String(16), nullable=False)
    aad_context: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ShopBinding(Base, TimestampMixin):
    __tablename__ = "shop_binding"
    __table_args__ = (
        UniqueConstraint("open_id", "shop_id", name="uq_shop_binding_open_shop"),
        CheckConstraint(
            "listing_mode IN ('LOCAL_REPLICATION','GLOBAL_LEGACY','UNKNOWN')",
            name="listing_mode_allowed",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    open_id: Mapped[str] = mapped_column(String(128), nullable=False)
    shop_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    shop_code: Mapped[str | None] = mapped_column(String(128))
    region: Mapped[str] = mapped_column(String(16), nullable=False)
    base_region: Mapped[str | None] = mapped_column(String(16))
    seller_type: Mapped[str | None] = mapped_column(String(64))
    shop_status: Mapped[str] = mapped_column(String(32), default="UNKNOWN", nullable=False)
    kyc_status: Mapped[str] = mapped_column(String(32), default="UNKNOWN", nullable=False)
    listing_mode: Mapped[str] = mapped_column(
        String(32), default=ListingMode.UNKNOWN.value, nullable=False
    )
    authorization_status: Mapped[str] = mapped_column(
        String(32), default=AuthorizationStatus.PENDING.value, nullable=False
    )
    shop_cipher_credential_id: Mapped[str | None] = mapped_column(
        ForeignKey("encrypted_credentials.id", ondelete="RESTRICT"), index=True
    )
    deauthorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScopeSnapshot(Base):
    __tablename__ = "scope_snapshots"
    __table_args__ = (Index("ix_scope_shop_captured", "shop_binding_id", "captured_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    shop_binding_id: Mapped[str] = mapped_column(
        ForeignKey("shop_binding.id", ondelete="CASCADE"), nullable=False
    )
    granted_scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    missing_scopes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ListingModeEvidence(Base):
    __tablename__ = "listing_mode_evidence"
    __table_args__ = (Index("ix_mode_evidence_shop_recorded", "shop_binding_id", "recorded_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    shop_binding_id: Mapped[str] = mapped_column(
        ForeignKey("shop_binding.id", ondelete="CASCADE"), nullable=False
    )
    evidence_source: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_value: Mapped[str] = mapped_column(String(255), nullable=False)
    supports_local: Mapped[bool | None] = mapped_column(Boolean)
    supports_global: Mapped[bool | None] = mapped_column(Boolean)
    read_only_endpoint: Mapped[str | None] = mapped_column(String(255))
    conflict: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ProductDraft(Base, TimestampMixin):
    __tablename__ = "product_drafts"
    __table_args__ = (
        UniqueConstraint("shop_binding_id", "payload_hash", name="uq_draft_shop_payload"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    shop_binding_id: Mapped[str | None] = mapped_column(
        ForeignKey("shop_binding.id", ondelete="CASCADE"), index=True
    )
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_result_id: Mapped[str | None] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    field_sources: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    unmapped_warnings: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="DRAFT", nullable=False)
    human_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ProductImageAsset(Base, TimestampMixin):
    __tablename__ = "product_image_assets"
    __table_args__ = (
        UniqueConstraint("product_draft_id", "source_ref_hash", name="uq_image_draft_source"),
        CheckConstraint("byte_size > 0", name="image_positive_size"),
        CheckConstraint(
            "upload_state IN ('VALIDATING','SUBMITTED','ACTIVE','FAILED')",
            name="image_upload_state_allowed",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    product_draft_id: Mapped[str] = mapped_column(
        ForeignKey("product_drafts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_ref_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    tiktok_image_id: Mapped[str | None] = mapped_column(String(255))
    upload_state: Mapped[str] = mapped_column(
        String(32), default=WriteState.VALIDATING.value, nullable=False
    )
    platform_request_id: Mapped[str | None] = mapped_column(String(128))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_redacted: Mapped[str | None] = mapped_column(Text)


class ProductLink(Base, TimestampMixin):
    __tablename__ = "product_links"
    __table_args__ = (
        UniqueConstraint("shop_binding_id", "seller_sku", name="uq_product_link_shop_sku"),
        UniqueConstraint(
            "shop_binding_id", "source_kind", "source_product_id", name="uq_product_link_source"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    shop_binding_id: Mapped[str] = mapped_column(
        ForeignKey("shop_binding.id", ondelete="CASCADE"), nullable=False
    )
    draft_id: Mapped[str | None] = mapped_column(
        ForeignKey("product_drafts.id", ondelete="SET NULL")
    )
    source_kind: Mapped[str | None] = mapped_column(String(32))
    source_product_id: Mapped[str | None] = mapped_column(String(128))
    seller_sku: Mapped[str] = mapped_column(String(128), nullable=False)
    global_product_id: Mapped[str | None] = mapped_column(String(128))
    local_product_by_region: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    last_synced_version: Mapped[str | None] = mapped_column(String(128))
    sync_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class MarketProductState(Base, TimestampMixin):
    __tablename__ = "market_product_states"
    __table_args__ = (
        UniqueConstraint("product_link_id", "region", name="uq_market_product_link_region"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    product_link_id: Mapped[str] = mapped_column(
        ForeignKey("product_links.id", ondelete="CASCADE"), nullable=False
    )
    region: Mapped[str] = mapped_column(String(16), nullable=False)
    local_product_id: Mapped[str | None] = mapped_column(String(128))
    product_status: Mapped[str] = mapped_column(String(32), default="DRAFT", nullable=False)
    operation_state: Mapped[str] = mapped_column(
        String(32), default=WriteState.VALIDATING.value, nullable=False
    )
    remote_version: Mapped[str | None] = mapped_column(String(128))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_redacted: Mapped[str | None] = mapped_column(Text)


class IdempotentOperation(Base, TimestampMixin):
    __tablename__ = "idempotent_operations"
    __table_args__ = (
        UniqueConstraint(
            "shop_binding_id",
            "operation",
            "business_key",
            "payload_hash",
            name="uq_idempotent_business_payload",
        ),
        UniqueConstraint(
            "shop_binding_id", "idempotency_key_hash", name="uq_idempotent_client_key"
        ),
        Index("ix_idempotent_due", "state", "next_attempt_at", "lease_until"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    shop_binding_id: Mapped[str] = mapped_column(
        ForeignKey("shop_binding.id", ondelete="CASCADE"), nullable=False
    )
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    business_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(
        String(32), default=WriteState.VALIDATING.value, nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    platform_request_id: Mapped[str | None] = mapped_column(String(128))
    result_reference: Mapped[str | None] = mapped_column(String(255))
    manual_review_reason: Mapped[str | None] = mapped_column(Text)


class QuotaSnapshotModel(Base):
    __tablename__ = "quota_snapshots"
    __table_args__ = (Index("ix_quota_shop_confirmed", "shop_binding_id", "confirmed_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    shop_binding_id: Mapped[str] = mapped_column(
        ForeignKey("shop_binding.id", ondelete="CASCADE"), nullable=False
    )
    region: Mapped[str] = mapped_column(String(16), nullable=False)
    stage: Mapped[str | None] = mapped_column(String(32))
    listing_limit: Mapped[int | None] = mapped_column(Integer)
    locally_submitted_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_by_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("admin_sessions.id", ondelete="SET NULL")
    )
    source: Mapped[str] = mapped_column(
        String(64), default="SELLER_CENTER_CONFIRMED", nullable=False
    )


class OrderRecord(Base, TimestampMixin):
    __tablename__ = "order_records"
    __table_args__ = (
        UniqueConstraint("shop_binding_id", "platform_order_id", name="uq_order_shop_platform"),
        Index("ix_order_shop_updated", "shop_binding_id", "source_updated_at"),
        CheckConstraint("item_count >= 0", name="order_nonnegative_item_count"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    shop_binding_id: Mapped[str] = mapped_column(
        ForeignKey("shop_binding.id", ondelete="CASCADE"), nullable=False
    )
    platform_order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    order_status: Mapped[str] = mapped_column(String(64), nullable=False)
    fulfillment_type: Mapped[str | None] = mapped_column(String(64))
    shipping_type: Mapped[str | None] = mapped_column(String(64))
    currency: Mapped[str | None] = mapped_column(String(8))
    total_amount: Mapped[str | None] = mapped_column(String(64))
    item_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detail_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    normalized_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class OrderLineRecord(Base, TimestampMixin):
    __tablename__ = "order_line_records"
    __table_args__ = (
        UniqueConstraint("order_record_id", "platform_line_id", name="uq_order_line_platform"),
        Index("ix_order_line_seller_sku", "seller_sku"),
        CheckConstraint("quantity > 0", name="order_line_positive_quantity"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    order_record_id: Mapped[str] = mapped_column(
        ForeignKey("order_records.id", ondelete="CASCADE"), nullable=False
    )
    platform_line_id: Mapped[str] = mapped_column(String(128), nullable=False)
    product_id: Mapped[str | None] = mapped_column(String(128))
    sku_id: Mapped[str | None] = mapped_column(String(128))
    seller_sku: Mapped[str | None] = mapped_column(String(128))
    line_status: Mapped[str | None] = mapped_column(String(64))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str | None] = mapped_column(String(8))
    sale_price: Mapped[str | None] = mapped_column(String(64))


class OrderSyncCheckpoint(Base, TimestampMixin):
    __tablename__ = "order_sync_checkpoints"
    __table_args__ = (
        UniqueConstraint("shop_binding_id", "stream_name", name="uq_order_checkpoint_stream"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    shop_binding_id: Mapped[str] = mapped_column(
        ForeignKey("shop_binding.id", ondelete="CASCADE"), nullable=False
    )
    stream_name: Mapped[str] = mapped_column(String(64), default="orders", nullable=False)
    page_token: Mapped[str | None] = mapped_column(String(512))
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class ScheduleJob(Base, TimestampMixin):
    __tablename__ = "schedule_jobs"
    __table_args__ = (
        Index("ix_schedule_due", "enabled", "next_run_at", "lease_until"),
        CheckConstraint("interval_seconds IS NULL OR interval_seconds > 0", name="positive_interval"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    shop_binding_id: Mapped[str] = mapped_column(
        ForeignKey("shop_binding.id", ondelete="CASCADE"), nullable=False
    )
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    schedule_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    interval_seconds: Mapped[int | None] = mapped_column(Integer)
    run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    required_scopes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    required_listing_mode: Mapped[str | None] = mapped_column(String(32))
    quota_cost: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScheduleRun(Base):
    __tablename__ = "schedule_runs"
    __table_args__ = (Index("ix_schedule_run_job_started", "schedule_job_id", "started_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    schedule_job_id: Mapped[str] = mapped_column(
        ForeignKey("schedule_jobs.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_redacted: Mapped[str | None] = mapped_column(Text)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_shop_created", "shop_binding_id", "created_at"),
        Index("ix_audit_request", "request_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("admin_sessions.id", ondelete="SET NULL")
    )
    shop_binding_id: Mapped[str | None] = mapped_column(
        ForeignKey("shop_binding.id", ondelete="SET NULL")
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(128))
    resource_type: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(128))
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    redacted_details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class RateLimitWindow(Base):
    __tablename__ = "rate_limit_windows"
    __table_args__ = (
        UniqueConstraint(
            "app_key_hash",
            "shop_id",
            "endpoint_key",
            "operation_type",
            "window_started_at",
            name="uq_rate_limit_partition_window",
        ),
        CheckConstraint("window_seconds > 0", name="positive_window"),
        CheckConstraint("used >= 0", name="nonnegative_used"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    app_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    shop_id: Mapped[str] = mapped_column(String(128), nullable=False)
    endpoint_key: Mapped[str] = mapped_column(String(128), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(16), nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    limit_value: Mapped[int | None] = mapped_column(Integer)
    used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )