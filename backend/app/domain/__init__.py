"""Immutable domain vocabulary shared by use cases and persistence adapters."""

from app.domain.enums import (
    AuthorizationStatus,
    ListingMode,
    MarketProductStatus,
    OperationKind,
    Scope,
    WriteState,
)
from app.domain.product import NormalizedImage, NormalizedProduct, NormalizedSku
from app.domain.quota import QuotaDecision, QuotaSnapshot, decide_listing_quota
from app.domain.scopes import ScopeGap, ScopeSet
from app.domain.state_machine import InvalidTransition, transition_write_state

__all__ = [
    "AuthorizationStatus",
    "InvalidTransition",
    "ListingMode",
    "MarketProductStatus",
    "NormalizedImage",
    "NormalizedProduct",
    "NormalizedSku",
    "OperationKind",
    "QuotaDecision",
    "QuotaSnapshot",
    "Scope",
    "ScopeGap",
    "ScopeSet",
    "WriteState",
    "decide_listing_quota",
    "transition_write_state",
]