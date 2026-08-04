"""Collector source composition root.

Only evidence-backed modes are registered: CJ through its documented V2 API,
1688 through its first-party Open Platform grant, and an explicitly selected
anonymous public offer-page reader. Neither official mode silently falls back to
public-page collection.
"""

from __future__ import annotations

from collector_app.outbound import SafeHttpClient
from collector_app.sources.alibaba_1688 import Alibaba1688PublicPageAdapter
from collector_app.sources.alibaba_1688_open import (
    Alibaba1688OpenPlatformAdapter,
    Alibaba1688OpenPlatformConfig,
)
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
    alibaba_1688_config: Alibaba1688OpenPlatformConfig | None = None,
    cj_http: SafeHttpClient | None = None,
    alibaba_1688_open_http: SafeHttpClient | None = None,
    alibaba_1688_http: SafeHttpClient | None = None,
) -> SourceRegistry:
    """Compose supported adapters without reading secrets from global state."""

    return build_source_registry(
        (
            Alibaba1688OpenPlatformAdapter(
                config=alibaba_1688_config,
                http=alibaba_1688_open_http,
            ),
            Alibaba1688PublicPageAdapter(http=alibaba_1688_http),
            CjOfficialApiAdapter(access_token=cj_access_token, http=cj_http),
        )
    )


__all__ = [
    "Alibaba1688OpenPlatformConfig",
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