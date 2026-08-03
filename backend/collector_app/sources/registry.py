"""Immutable source registry; job input cannot choose arbitrary implementation code."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from collector_app.sources.contracts import SourceAdapter, SourceAdapterError, SourceMode, SourceRequest


@dataclass(frozen=True, slots=True, order=True)
class SourceKey:
    source: str
    mode: SourceMode

    @classmethod
    def of(cls, source: str, mode: SourceMode) -> SourceKey:
        normalized = source.strip().upper()
        if not normalized:
            raise ValueError("source is required")
        return cls(normalized, mode)


@dataclass(frozen=True, slots=True)
class SourceRegistry:
    _adapters: Mapping[SourceKey, SourceAdapter]

    def resolve(self, request: SourceRequest) -> SourceAdapter:
        key = SourceKey.of(request.source, request.mode)
        adapter = self._adapters.get(key)
        if adapter is None:
            raise SourceAdapterError(
                "unsupported_source",
                "source and mode are not registered",
            )
        return adapter

    @property
    def available(self) -> tuple[SourceKey, ...]:
        return tuple(sorted(self._adapters))



def build_source_registry(adapters: Iterable[SourceAdapter]) -> SourceRegistry:
    """Build once at composition root and reject ambiguous registrations."""

    registered: dict[SourceKey, SourceAdapter] = {}
    for adapter in adapters:
        if not isinstance(adapter, SourceAdapter):
            raise TypeError("adapter does not implement the source contract")
        key = SourceKey.of(adapter.source, adapter.mode)
        if key in registered:
            raise ValueError(f"duplicate source adapter: {key.source}/{key.mode.value}")
        registered[key] = adapter
    if not registered:
        raise ValueError("at least one source adapter is required")
    return SourceRegistry(MappingProxyType(registered))