"""Fixture-only Collector process used by the loopback HTTP end-to-end test."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path

import uvicorn
from PIL import Image

import collector_app.main as collector_main
from collector_app.images import StoredImage
from collector_app.sources import (
    SourceArtifact,
    SourceMode,
    SourceRequest,
    build_source_registry,
)
from collector_app.sources.intents import normalize_source_identity
from shared.safe_paths import COLLECTOR_IMAGE_DIR, PROJECT_ROOT, ensure_runtime_directories


class _FixtureCjAdapter:
    source = "CJ"
    mode = SourceMode.OFFICIAL_API

    async def collect(self, request: SourceRequest) -> SourceArtifact:
        identity = normalize_source_identity(
            source=request.source,
            mode=request.mode,
            source_url=request.source_url,
        )
        document = {
            "code": 200,
            "data": {
                "pid": identity.source_product_id,
                "productNameEn": "Independent Process Product",
                "description": "<p>Collected through the worker process.</p>",
                "bigImage": "https://cf.cjdropshipping.com/e2e-fixture.png",
                "productImageSet": [],
                "variants": [
                    {
                        "variantSku": "E2E-SKU-1",
                        "variantSellPrice": "9.99",
                        "variantKey": "red",
                    }
                ],
            },
        }
        return SourceArtifact(
            source=self.source,
            mode=self.mode,
            canonical_url=request.source_url,
            source_product_id=identity.source_product_id,
            media_type="application/json",
            body=json.dumps(document, separators=(",", ":")).encode("utf-8"),
        )


class _FixtureImageDownloader:
    def __init__(self) -> None:
        selected = os.environ["COLLECTOR_E2E_IMAGE_NAME"]
        if Path(selected).name != selected or not selected.endswith(".png"):
            raise ValueError("fixture image name is invalid")
        self._path = COLLECTOR_IMAGE_DIR / selected

    async def download(self, *, source: str, url: str) -> StoredImage:
        if source != "CJ" or url != "https://cf.cjdropshipping.com/e2e-fixture.png":
            raise ValueError("fixture received an unexpected image identity")
        ensure_runtime_directories()
        target = io.BytesIO()
        Image.new("RGBA", (1, 1), (0, 128, 255, 255)).save(target, format="PNG")
        content = target.getvalue()
        self._path.write_bytes(content)
        return StoredImage(
            relative_path=self._path.relative_to(PROJECT_ROOT).as_posix(),
            sha256=hashlib.sha256(content).hexdigest(),
            content_type="image/png",
            byte_size=len(content),
            width=1,
            height=1,
        )

    async def discard(self, image: StoredImage) -> bool:
        expected = self._path.relative_to(PROJECT_ROOT).as_posix()
        if image.relative_path != expected:
            raise ValueError("fixture received an unexpected stored image")
        if not self._path.exists():
            return False
        self._path.unlink()
        return True


def _registry(**_configuration: object) -> object:
    return build_source_registry((_FixtureCjAdapter(),))


def _images() -> object:
    return _FixtureImageDownloader()


collector_main.default_source_registry = _registry
collector_main.default_image_downloader = _images
app = collector_main.create_app(start_worker=True)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8010,
        log_level="warning",
        access_log=False,
    )