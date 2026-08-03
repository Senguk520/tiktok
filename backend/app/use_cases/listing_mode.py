"""Read-only listing-mode evidence aggregation with fail-closed writes."""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.enums import ListingMode, Scope
from app.domain.scopes import ScopeSet


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


@dataclass(frozen=True, slots=True)
class ListingModeDecision:
    mode: ListingMode
    evidence: tuple[str, ...]
    blockers: tuple[str, ...]

    @property
    def writable(self) -> bool:
        return self.mode is not ListingMode.UNKNOWN and not self.blockers


def determine_listing_mode(facts: ListingModeFacts) -> ListingModeDecision:
    candidates: set[ListingMode] = set()
    evidence: list[str] = []
    blockers: list[str] = []
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
        return ListingModeDecision(ListingMode.UNKNOWN, tuple(evidence), tuple(blockers))
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