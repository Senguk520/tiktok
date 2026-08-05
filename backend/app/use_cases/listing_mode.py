"""Read-only listing-mode evidence aggregation with fail-closed writes."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ListingModeEvidence, ScopeSnapshot, ShopBinding
from app.domain.enums import AuthorizationStatus, ListingMode, Scope, WriteState
from app.domain.scopes import ScopeSet
from app.integrations.tiktok.endpoints import ENDPOINTS
from app.repositories.audit import record_audit_fact
from app.repositories.idempotency import (
    IdempotencyConflict,
    IdempotencyRequest,
    canonical_payload_hash,
    register_operation,
)


class ListingModeBlocked(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class ListingModeFacts:
    reported_mode: ListingMode | None = None
    migration_completed: bool | None = None
    global_program_eligible: bool | None = None
    operator_confirmed_mode: ListingMode | None = None
    local_read_verified: bool = False
    global_read_verified: bool = False
    scopes: ScopeSet = ScopeSet(frozenset())
    conflicting_facts: bool = False


@dataclass(frozen=True, slots=True)
class ListingModeDecision:
    mode: ListingMode
    evidence: tuple[str, ...]
    blockers: tuple[str, ...]

    @property
    def writable(self) -> bool:
        return self.mode is not ListingMode.UNKNOWN and not self.blockers


@dataclass(frozen=True, slots=True)
class ManualListingModeConfirmation:
    target_shop_id: str
    mode: ListingMode
    local_read_verified: bool
    global_read_verified: bool


@dataclass(frozen=True, slots=True)
class PersistedListingModeDecision:
    decision: ListingModeDecision
    recorded_evidence_id: str | None = None
    replayed: bool = False


def determine_listing_mode(facts: ListingModeFacts) -> ListingModeDecision:
    candidates: set[ListingMode] = set()
    evidence: list[str] = []
    blockers: list[str] = []
    if facts.conflicting_facts:
        return ListingModeDecision(
            ListingMode.UNKNOWN,
            (),
            ("conflicting listing-mode evidence",),
        )
    if facts.reported_mode is not None and facts.reported_mode is not ListingMode.UNKNOWN:
        candidates.add(facts.reported_mode)
        evidence.append(f"authorized_shop:{facts.reported_mode.value}")
    if facts.operator_confirmed_mode is not None and facts.operator_confirmed_mode is not ListingMode.UNKNOWN:
        candidates.add(facts.operator_confirmed_mode)
        evidence.append(f"operator_confirmation:{facts.operator_confirmed_mode.value}")
    if facts.migration_completed is True:
        candidates.add(ListingMode.LOCAL_REPLICATION)
        evidence.append("migration_completed:true")
    elif facts.migration_completed is False and facts.global_program_eligible is True:
        candidates.add(ListingMode.GLOBAL_LEGACY)
        evidence.append("migration_completed:false+global_eligible:true")
    if len(candidates) != 1:
        reason = "conflicting listing-mode evidence" if len(candidates) > 1 else "no conclusive mode evidence"
        return ListingModeDecision(ListingMode.UNKNOWN, tuple(evidence), (reason,))

    mode = next(iter(candidates))
    if mode is ListingMode.LOCAL_REPLICATION:
        gap = facts.scopes.gap([Scope.PRODUCT_BASIC, Scope.PRODUCT_WRITE])
        if gap.missing:
            blockers.append("missing local product scopes")
        if not facts.local_read_verified:
            blockers.append("local read capability not verified")
    else:
        gap = facts.scopes.gap(
            [Scope.GLOBAL_PRODUCT_INFO, Scope.GLOBAL_PRODUCT_WRITE, Scope.PRODUCT_BASIC]
        )
        if gap.missing:
            blockers.append("missing global legacy scopes")
        if not facts.global_read_verified:
            blockers.append("global read capability not verified")
    if blockers:
        return ListingModeDecision(mode, tuple(evidence), tuple(blockers))
    return ListingModeDecision(mode, tuple(evidence), ())


def assert_listing_write_allowed(
    decision: ListingModeDecision,
    *,
    expected_mode: ListingMode | None = None,
) -> ListingMode:
    if not decision.writable:
        raise ListingModeBlocked("listing mode is not conclusively verified")
    if expected_mode is not None and decision.mode is not expected_mode:
        raise ListingModeBlocked("requested listing gateway does not match verified mode")
    return decision.mode


def _mode_value(value: str) -> ListingMode | None:
    try:
        selected = ListingMode(value)
    except ValueError:
        return None
    return selected if selected is not ListingMode.UNKNOWN else None


def _bool_value(value: str) -> bool | None:
    normalized = value.strip().upper()
    if normalized == "TRUE":
        return True
    if normalized == "FALSE":
        return False
    return None


def _facts_from_persisted(
    evidence_rows: tuple[ListingModeEvidence, ...],
    scopes: ScopeSet,
) -> ListingModeFacts:
    reported_modes: set[ListingMode] = set()
    operator_modes: set[ListingMode] = set()
    migration_values: set[bool] = set()
    global_eligibility_values: set[bool] = set()
    local_read_verified = False
    global_read_verified = False
    for row in evidence_rows:
        source = row.evidence_source.strip().upper()
        mode = _mode_value(row.observed_value)
        if source == "AUTHORIZED_SHOP_MODE" and mode is not None:
            reported_modes.add(mode)
        elif source == "OPERATOR_TARGET_ACCOUNT_CONFIRMATION" and mode is not None:
            operator_modes.add(mode)
        elif source == "MIGRATION_COMPLETED":
            value = _bool_value(row.observed_value)
            if value is not None:
                migration_values.add(value)
        elif source == "GLOBAL_PROGRAM_ELIGIBLE":
            value = _bool_value(row.observed_value)
            if value is not None:
                global_eligibility_values.add(value)
        local_read_verified = local_read_verified or row.supports_local is True
        global_read_verified = global_read_verified or row.supports_global is True

    reported_mode = next(iter(reported_modes)) if len(reported_modes) == 1 else None
    operator_mode = next(iter(operator_modes)) if len(operator_modes) == 1 else None
    migration_completed = next(iter(migration_values)) if len(migration_values) == 1 else None
    global_eligible = (
        next(iter(global_eligibility_values)) if len(global_eligibility_values) == 1 else None
    )
    conflicting_facts = any(row.conflict for row in evidence_rows) or any(
        len(values) > 1
        for values in (
            reported_modes,
            operator_modes,
            migration_values,
            global_eligibility_values,
        )
    )
    return ListingModeFacts(
        reported_mode=reported_mode,
        migration_completed=migration_completed,
        global_program_eligible=global_eligible,
        operator_confirmed_mode=operator_mode,
        local_read_verified=local_read_verified,
        global_read_verified=global_read_verified,
        scopes=scopes,
        conflicting_facts=conflicting_facts,
    )


async def _persisted_listing_mode_facts(
    session: AsyncSession,
    *,
    shop_binding_id: str,
) -> tuple[ShopBinding, ListingModeFacts]:
    binding = await session.get(ShopBinding, shop_binding_id)
    if binding is None:
        raise ListingModeBlocked("shop binding was not found")
    snapshot = await session.scalar(
        select(ScopeSnapshot)
        .where(ScopeSnapshot.shop_binding_id == binding.id)
        .order_by(ScopeSnapshot.captured_at.desc(), ScopeSnapshot.id.desc())
        .limit(1)
    )
    rows = tuple(
        await session.scalars(
            select(ListingModeEvidence)
            .where(ListingModeEvidence.shop_binding_id == binding.id)
            .order_by(ListingModeEvidence.recorded_at, ListingModeEvidence.id)
        )
    )
    scopes = ScopeSet.parse(snapshot.granted_scopes) if snapshot is not None else ScopeSet(frozenset())
    return binding, _facts_from_persisted(rows, scopes)


async def assess_persisted_listing_mode(
    session: AsyncSession,
    *,
    shop_binding_id: str,
) -> ListingModeDecision:
    _binding, facts = await _persisted_listing_mode_facts(
        session,
        shop_binding_id=shop_binding_id,
    )
    return determine_listing_mode(facts)


async def confirm_manual_listing_mode(
    session: AsyncSession,
    *,
    shop_binding_id: str,
    actor_session_id: str,
    idempotency_key: str,
    confirmation: ManualListingModeConfirmation,
) -> PersistedListingModeDecision:
    binding = await session.get(ShopBinding, shop_binding_id)
    if binding is None:
        raise ListingModeBlocked("shop binding was not found")
    if binding.authorization_status != AuthorizationStatus.ACTIVE.value:
        raise ListingModeBlocked("shop authorization is not active")
    if confirmation.target_shop_id != binding.shop_id:
        raise ListingModeBlocked("confirmed target account does not match the authorized shop")
    if confirmation.mode is ListingMode.UNKNOWN:
        raise ValueError("manual confirmation must select a conclusive listing mode")
    if confirmation.mode is ListingMode.LOCAL_REPLICATION:
        if not confirmation.local_read_verified or confirmation.global_read_verified:
            raise ValueError("local confirmation requires only the verified local read capability")
    elif not confirmation.global_read_verified or confirmation.local_read_verified:
        raise ValueError("global confirmation requires only the verified global read capability")

    intent = {
        "target_shop_id": confirmation.target_shop_id,
        "mode": confirmation.mode.value,
        "local_read_verified": confirmation.local_read_verified,
        "global_read_verified": confirmation.global_read_verified,
    }
    operation, created = await register_operation(
        session,
        IdempotencyRequest(
            shop_binding_id=binding.id,
            operation="CONFIRM_LISTING_MODE",
            business_key=hashlib.sha256(
                f"listing-mode-confirmation:{idempotency_key}".encode()
            ).hexdigest(),
            payload_hash=canonical_payload_hash(intent),
            idempotency_key=idempotency_key,
        ),
    )
    if not created:
        if operation.state != WriteState.ACTIVE.value or operation.result_reference is None:
            raise IdempotencyConflict("listing mode confirmation is incomplete")
        recorded = await session.get(ListingModeEvidence, operation.result_reference)
        if recorded is None or recorded.shop_binding_id != binding.id:
            raise IdempotencyConflict("listing mode confirmation result is inconsistent")
        decision = await assess_persisted_listing_mode(
            session,
            shop_binding_id=binding.id,
        )
        return PersistedListingModeDecision(
            decision=decision,
            recorded_evidence_id=recorded.id,
            replayed=True,
        )

    operation.state = WriteState.SUBMITTED.value
    evidence = ListingModeEvidence(
        shop_binding_id=binding.id,
        evidence_source="OPERATOR_TARGET_ACCOUNT_CONFIRMATION",
        observed_value=confirmation.mode.value,
        supports_local=confirmation.local_read_verified,
        supports_global=confirmation.global_read_verified,
        read_only_endpoint=(
            ENDPOINTS["local.search"].path
            if confirmation.mode is ListingMode.LOCAL_REPLICATION
            else ENDPOINTS["global.search"].path
        ),
        conflict=False,
    )
    session.add(evidence)
    await session.flush()
    decision = await assess_persisted_listing_mode(
        session,
        shop_binding_id=binding.id,
    )
    evidence.conflict = decision.mode is ListingMode.UNKNOWN and (
        "conflicting listing-mode evidence" in decision.blockers
    )
    binding.listing_mode = decision.mode.value
    operation.state = WriteState.ACTIVE.value
    operation.result_reference = evidence.id
    operation.manual_review_reason = (
        "listing mode evidence conflicts with an earlier confirmation"
        if evidence.conflict
        else None
    )
    await record_audit_fact(
        session,
        actor_session_id=actor_session_id,
        shop_binding_id=binding.id,
        event_type="listing_mode.confirmed",
        resource_type="listing_mode_evidence",
        resource_id=evidence.id,
        outcome="SUCCESS" if decision.writable else "BLOCKED",
        details={
            "code": f"listing_mode_{decision.mode.value.lower()}",
            "reason": "manual_target_account_confirmation",
        },
    )
    return PersistedListingModeDecision(
        decision=decision,
        recorded_evidence_id=evidence.id,
        replayed=False,
    )