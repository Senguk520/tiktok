"""Scope-set algebra; missing capabilities are computed before every use case."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.domain.enums import Scope


@dataclass(frozen=True, slots=True)
class ScopeGap:
    required: frozenset[Scope]
    granted: frozenset[Scope]

    @property
    def missing(self) -> frozenset[Scope]:
        return self.required - self.granted

    @property
    def allowed(self) -> bool:
        return not self.missing


@dataclass(frozen=True, slots=True)
class ScopeSet:
    values: frozenset[Scope]

    @classmethod
    def parse(cls, values: Iterable[str | Scope]) -> ScopeSet:
        parsed: set[Scope] = set()
        for value in values:
            try:
                parsed.add(Scope(value))
            except ValueError:
                # Unknown upstream scopes do not become local capabilities.
                continue
        return cls(frozenset(parsed))

    def gap(self, required: Iterable[Scope]) -> ScopeGap:
        return ScopeGap(required=frozenset(required), granted=self.values)

    def require(self, required: Iterable[Scope]) -> None:
        gap = self.gap(required)
        if not gap.allowed:
            missing = ", ".join(sorted(item.value for item in gap.missing))
            raise PermissionError(f"missing TikTok scopes: {missing}")