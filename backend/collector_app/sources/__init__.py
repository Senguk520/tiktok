"""Collector source composition root.

Only evidence-backed modes are registered: CJ through its documented V2 API and
1688 through an anonymous public offer page. An unverified 1688 private API mode
is deliberately absent and therefore fails closed in the registry.
"""

from __future__ import annotations

from collector_app.outbound import SafeHttpClient
from collector_app.sources.alibaba_1688 import Alibaba1688PublicPageAdapter
from collector_app.sources.cj import CjOfficialApiAdapter
from collector_app.sources.contracts import (
    SourceAdapter,
    SourceAdapterError,
    SourceArtifact,
    SourceMode,
    SourceRequest,
)
from collector_app.sources.registry import SourceKey, SourceRegistry, build_source_registry


def default_source_registry(
    *,
    cj_access_token: str | None,
    cj_http: SafeHttpClient | None = None,
    alibaba_1688_http: SafeHttpClient | None = None,
) -> SourceRegistry:
    """Compose supported adapters without reading secrets from global state."""

    return build_source_registry(
        (
            CjOfficialApiAdapter(access_token=cj_access_token, http=cj_http),
            Alibaba1688PublicPageAdapter(http=alibaba_1688_http),
        )
    )


__all__ = [
    "SourceAdapter",
    "SourceAdapterError",
    "SourceArtifact",
    "SourceKey",
    "SourceMode",
    "SourceRegistry",
    "SourceRequest",
    "build_source_registry",
    "default_source_registry",
]