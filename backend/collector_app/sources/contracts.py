"""Source adapter contracts independent from persistence and worker orchestration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable


class SourceMode(StrEnum):
    OFFICIAL_API = "OFFICIAL_API"
    PUBLIC_PAGE = "PUBLIC_PAGE"


@dataclass(frozen=True, slots=True)
class SourceRequest:
    """Validated job intent presented to exactly one registered adapter."""

    source: str
    mode: SourceMode
    source_url: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        source = self.source.strip().upper()
        source_url = self.source_url.strip()
        try:
            mode = SourceMode(self.mode)
        except ValueError as exc:
            raise ValueError("unsupported source mode") from exc
        if not source or not source_url:
            raise ValueError("source and source URL are required")
        if any(not isinstance(key, str) or not key.strip() for key in self.payload):
            raise ValueError("source payload keys must be non-empty strings")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "source_url", source_url)
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class SourceArtifact:
    """Bounded upstream fact; normalization belongs to the worker/import layer."""

    source: str
    mode: SourceMode
    canonical_url: str
    source_product_id: str | None
    media_type: str
    body: bytes

    def __post_init__(self) -> None:
        source = self.source.strip().upper()
        canonical_url = self.canonical_url.strip()
        media_type = self.media_type.strip().lower()
        try:
            mode = SourceMode(self.mode)
        except ValueError as exc:
            raise ValueError("unsupported source mode") from exc
        if not source or not canonical_url or not media_type:
            raise ValueError("artifact source, URL and media type are required")
        if not self.body:
            raise ValueError("source artifact cannot be empty")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "canonical_url", canonical_url)
        object.__setattr__(self, "media_type", media_type)


class SourceAdapterError(RuntimeError):
    """Stable adapter failure safe to persist after redaction."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@runtime_checkable
class SourceAdapter(Protocol):
    source: str
    mode: SourceMode

    async def collect(self, request: SourceRequest) -> SourceArtifact:
        """Fetch one bounded source fact or raise ``SourceAdapterError``."""