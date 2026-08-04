"""Controlled image acquisition and atomic Collector-owned storage."""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import stat
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from collector_app.outbound import OutboundPolicy, OutboundRequestError, SafeHttpClient
from collector_app.sources.contracts import SourceAdapterError
from shared.safe_paths import (
    COLLECTOR_IMAGE_DIR,
    PROJECT_ROOT,
    InvalidImageError,
    UnsafePathError,
    ensure_runtime_directories,
    resolve_collector_image_path,
    resolve_project_path,
)

_IMAGE_EXTENSION = MappingProxyType(
    {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
)
_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_MAX_DIMENSION = 12_000
_MAX_PIXELS = 40_000_000


@dataclass(frozen=True, slots=True)
class StoredImage:
    relative_path: str
    sha256: str
    content_type: str
    byte_size: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class ImageTransformPolicy:
    """Explicit, deterministic image edits; the default preserves source bytes."""

    center_square_crop: bool = False
    watermark_text: str | None = None
    jpeg_quality: int = 90

    def __post_init__(self) -> None:
        watermark = (self.watermark_text or "").strip()
        if len(watermark) > 120 or any(ord(character) < 32 for character in watermark):
            raise ValueError("watermark text is invalid")
        if not 75 <= self.jpeg_quality <= 95:
            raise ValueError("JPEG quality must be between 75 and 95")
        object.__setattr__(self, "watermark_text", watermark or None)

    @property
    def enabled(self) -> bool:
        return self.center_square_crop or self.watermark_text is not None


@dataclass(frozen=True, slots=True)
class ImageSourcePolicy:
    source: str
    allowed_hosts: frozenset[str]
    max_response_bytes: int = _MAX_IMAGE_BYTES

    def __post_init__(self) -> None:
        source = self.source.strip().upper()
        if not source or not self.allowed_hosts:
            raise ValueError("image source and explicit hosts are required")
        object.__setattr__(self, "source", source)


class ImageDownloader:
    """Select a source-owned client; callers cannot supply arbitrary hosts."""

    def __init__(
        self,
        clients: Mapping[str, SafeHttpClient],
        *,
        transform: ImageTransformPolicy | None = None,
    ) -> None:
        normalized = {source.strip().upper(): client for source, client in clients.items()}
        if not normalized or any(not source for source in normalized):
            raise ValueError("at least one image source client is required")
        self._clients = MappingProxyType(normalized)
        self._transform = transform or ImageTransformPolicy()

    async def download(self, *, source: str, url: str) -> StoredImage:
        client = self._clients.get(source.strip().upper())
        if client is None:
            raise SourceAdapterError("image_source_not_allowed", "image source is not configured")
        try:
            response = await client.get(
                url,
                headers={
                    "Accept": "image/jpeg,image/png,image/webp,image/gif",
                    "User-Agent": "single-shop-collector/0.1",
                },
            )
        except OutboundRequestError as exc:
            raise SourceAdapterError(exc.code, str(exc), retryable=exc.retryable) from exc
        if response.status_code == 429:
            raise SourceAdapterError("source_rate_limited", "image source rate limit was reached", retryable=True)
        if response.status_code >= 500:
            raise SourceAdapterError("source_unavailable", "image source is unavailable", retryable=True)
        if response.status_code != 200:
            raise SourceAdapterError("image_request_rejected", "image source rejected the request")

        declared_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        source_type, _source_width, _source_height = inspect_image(response.content)
        if declared_type != source_type:
            raise SourceAdapterError(
                "image_type_mismatch",
                "image content does not match its declared type",
            )
        stored_content = transform_image(
            response.content,
            content_type=source_type,
            policy=self._transform,
        )
        content_type, width, height = inspect_image(stored_content)
        digest = hashlib.sha256(stored_content).hexdigest()
        try:
            relative_path = await asyncio.to_thread(
                _atomic_store,
                stored_content,
                _IMAGE_EXTENSION[content_type],
            )
        except (InvalidImageError, UnsafePathError) as exc:
            raise SourceAdapterError(
                "image_path_invalid",
                "image storage path is outside its approved boundary",
            ) from exc
        return StoredImage(
            relative_path=relative_path,
            sha256=digest,
            content_type=content_type,
            byte_size=len(stored_content),
            width=width,
            height=height,
        )

    async def discard(self, image: StoredImage) -> bool:
        try:
            return await asyncio.to_thread(_safe_discard, image.relative_path)
        except (InvalidImageError, UnsafePathError) as exc:
            raise SourceAdapterError(
                "image_path_invalid",
                "image storage path is outside its approved boundary",
            ) from exc


def default_image_downloader(
    *,
    transform: ImageTransformPolicy | None = None,
) -> ImageDownloader:
    """Build evidence-backed, exact CDN host policies for supported sources."""

    policies = (
        ImageSourcePolicy("CJ", frozenset({"cf.cjdropshipping.com"})),
        ImageSourcePolicy(
            "1688",
            frozenset(
                {
                    "cbu01.alicdn.com",
                    "cbu02.alicdn.com",
                    "cbu03.alicdn.com",
                    "cbu04.alicdn.com",
                    "img.alicdn.com",
                }
            ),
        ),
    )
    return ImageDownloader(
        {
            policy.source: SafeHttpClient(
                OutboundPolicy(
                    allowed_hosts=policy.allowed_hosts,
                    max_response_bytes=policy.max_response_bytes,
                    max_redirects=2,
                )
            )
            for policy in policies
        },
        transform=transform,
    )


def transform_image(
    content: bytes,
    *,
    content_type: str,
    policy: ImageTransformPolicy,
) -> bytes:
    """Apply explicitly configured edits and strip untrusted image metadata."""

    if not policy.enabled:
        return content
    if content_type == "image/gif":
        raise SourceAdapterError(
            "image_transform_unsupported",
            "animated GIF transformation is not supported",
        )
    formats = {
        "image/jpeg": "JPEG",
        "image/png": "PNG",
        "image/webp": "WEBP",
    }
    output_format = formats.get(content_type)
    if output_format is None:
        raise SourceAdapterError("image_transform_unsupported", "image format cannot be transformed")
    try:
        with Image.open(io.BytesIO(content)) as opened:
            if getattr(opened, "n_frames", 1) != 1:
                raise SourceAdapterError(
                    "image_transform_unsupported",
                    "animated image transformation is not supported",
                )
            opened.load()
            image = ImageOps.exif_transpose(opened)
            if image.width * image.height > _MAX_PIXELS:
                raise SourceAdapterError(
                    "image_dimensions_invalid",
                    "image dimensions exceed safe limits",
                )
            if policy.center_square_crop:
                side = min(image.width, image.height)
                left = (image.width - side) // 2
                top = (image.height - side) // 2
                image = image.crop((left, top, left + side, top + side))
            if policy.watermark_text:
                image = _watermark(image, policy.watermark_text)
            if output_format == "JPEG":
                image = image.convert("RGB")
            elif image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA")
            target = io.BytesIO()
            save_options: dict[str, int | bool] = {"optimize": True}
            if output_format in {"JPEG", "WEBP"}:
                save_options["quality"] = policy.jpeg_quality
            image.save(target, format=output_format, **save_options)
    except SourceAdapterError:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError) as exc:
        raise SourceAdapterError(
            "image_transform_failed",
            "image could not be safely transformed",
        ) from exc
    transformed = target.getvalue()
    if len(transformed) > _MAX_IMAGE_BYTES:
        raise SourceAdapterError("image_size_invalid", "transformed image exceeds the size limit")
    transformed_type, _width, _height = inspect_image(transformed)
    if transformed_type != content_type:
        raise SourceAdapterError("image_type_mismatch", "transformed image type changed unexpectedly")
    return transformed


def _watermark(image: Image.Image, text: str) -> Image.Image:
    canvas = image.convert("RGBA")
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = ImageFont.load_default()
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    text_width = right - left
    text_height = bottom - top
    margin = max(4, min(canvas.size) // 100)
    x = max(margin, canvas.width - text_width - margin * 2)
    y = max(margin, canvas.height - text_height - margin * 2)
    draw.rounded_rectangle(
        (x - margin, y - margin, x + text_width + margin, y + text_height + margin),
        radius=max(2, margin // 2),
        fill=(0, 0, 0, 150),
    )
    draw.text((x, y), text, fill=(255, 255, 255, 220), font=font)
    return Image.alpha_composite(canvas, overlay)


def inspect_image(content: bytes) -> tuple[str, int, int]:
    """Validate one complete image container without invoking an image decoder."""

    if not content or len(content) > _MAX_IMAGE_BYTES:
        raise SourceAdapterError("image_size_invalid", "image is empty or exceeds the size limit")
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        content_type, dimensions = "image/png", _png_dimensions(content)
    elif content.startswith((b"GIF87a", b"GIF89a")):
        content_type, dimensions = "image/gif", _gif_dimensions(content)
    elif content.startswith(b"RIFF") and len(content) >= 12 and content[8:12] == b"WEBP":
        content_type, dimensions = "image/webp", _webp_dimensions(content)
    elif content.startswith(b"\xff\xd8\xff"):
        content_type, dimensions = "image/jpeg", _jpeg_dimensions(content)
    else:
        raise SourceAdapterError("image_format_invalid", "image format is not allowed")
    width, height = dimensions
    if (
        width <= 0
        or height <= 0
        or width > _MAX_DIMENSION
        or height > _MAX_DIMENSION
        or width * height > _MAX_PIXELS
    ):
        raise SourceAdapterError("image_dimensions_invalid", "image dimensions exceed safe limits")
    return content_type, width, height


def _png_dimensions(content: bytes) -> tuple[int, int]:
    if len(content) < 45 or not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise SourceAdapterError("image_container_invalid", "PNG container is invalid")
    position = 8
    dimensions: tuple[int, int] | None = None
    chunk_index = 0
    while position + 12 <= len(content):
        length = int.from_bytes(content[position : position + 4], "big")
        chunk_type = content[position + 4 : position + 8]
        data_start = position + 8
        data_end = data_start + length
        chunk_end = data_end + 4
        if chunk_end > len(content):
            break
        expected_crc = int.from_bytes(content[data_end:chunk_end], "big")
        actual_crc = zlib.crc32(chunk_type + content[data_start:data_end]) & 0xFFFFFFFF
        if expected_crc != actual_crc:
            raise SourceAdapterError("image_container_invalid", "PNG chunk checksum is invalid")
        if chunk_index == 0:
            if chunk_type != b"IHDR" or length != 13:
                break
            dimensions = (
                int.from_bytes(content[data_start : data_start + 4], "big"),
                int.from_bytes(content[data_start + 4 : data_start + 8], "big"),
            )
        if chunk_type == b"IEND":
            if length != 0 or chunk_end != len(content) or dimensions is None:
                break
            return dimensions
        position = chunk_end
        chunk_index += 1
    raise SourceAdapterError("image_container_invalid", "PNG container is invalid")


def _gif_dimensions(content: bytes) -> tuple[int, int]:
    if len(content) < 14 or not content.endswith(b";"):
        raise SourceAdapterError("image_container_invalid", "GIF container is invalid")
    return int.from_bytes(content[6:8], "little"), int.from_bytes(content[8:10], "little")


def _webp_dimensions(content: bytes) -> tuple[int, int]:
    if len(content) < 30 or int.from_bytes(content[4:8], "little") + 8 != len(content):
        raise SourceAdapterError("image_container_invalid", "WebP container is invalid")
    kind = content[12:16]
    if kind == b"VP8X":
        return 1 + int.from_bytes(content[24:27], "little"), 1 + int.from_bytes(
            content[27:30], "little"
        )
    if kind == b"VP8 " and content[23:26] == b"\x9d\x01\x2a":
        return int.from_bytes(content[26:28], "little") & 0x3FFF, int.from_bytes(
            content[28:30], "little"
        ) & 0x3FFF
    if kind == b"VP8L" and content[20] == 0x2F and len(content) >= 25:
        bits = int.from_bytes(content[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    raise SourceAdapterError("image_container_invalid", "WebP dimensions are invalid")


def _jpeg_dimensions(content: bytes) -> tuple[int, int]:
    if len(content) < 8 or not content.endswith(b"\xff\xd9"):
        raise SourceAdapterError("image_container_invalid", "JPEG container is invalid")
    start_of_frame = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    position = 2
    while position + 4 <= len(content):
        if content[position] != 0xFF:
            raise SourceAdapterError("image_container_invalid", "JPEG marker sequence is invalid")
        while position < len(content) and content[position] == 0xFF:
            position += 1
        if position >= len(content):
            break
        marker = content[position]
        position += 1
        if marker in {0x01, 0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if position + 2 > len(content):
            break
        segment_length = int.from_bytes(content[position : position + 2], "big")
        if segment_length < 2 or position + segment_length > len(content):
            raise SourceAdapterError("image_container_invalid", "JPEG segment is invalid")
        if marker in start_of_frame:
            if segment_length < 7:
                break
            return (
                int.from_bytes(content[position + 5 : position + 7], "big"),
                int.from_bytes(content[position + 3 : position + 5], "big"),
            )
        if marker == 0xDA:
            break
        position += segment_length
    raise SourceAdapterError("image_container_invalid", "JPEG dimensions are missing")


def _atomic_store(content: bytes, extension: str) -> str:
    ensure_runtime_directories()
    final_path = resolve_collector_image_path(COLLECTOR_IMAGE_DIR / f"{uuid4()}{extension}")
    partial_path = resolve_project_path(final_path.with_name(f".{final_path.name}.part"))
    if partial_path.parent != COLLECTOR_IMAGE_DIR.resolve(strict=False):
        raise SourceAdapterError("image_path_invalid", "image path escaped its runtime directory")
    try:
        with partial_path.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if final_path.exists():
            raise SourceAdapterError("image_path_conflict", "image target already exists")
        os.replace(partial_path, final_path)
        resolve_collector_image_path(final_path)
    except SourceAdapterError:
        _unlink_regular(partial_path)
        raise
    except OSError as exc:
        _unlink_regular(partial_path)
        raise SourceAdapterError("image_storage_failed", "image could not be stored", retryable=True) from exc
    return final_path.relative_to(PROJECT_ROOT.resolve(strict=True)).as_posix()


def _safe_discard(relative_path: str) -> bool:
    path = resolve_collector_image_path(relative_path)
    return _unlink_regular(path)


def _unlink_regular(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise SourceAdapterError("image_storage_failed", "image could not be inspected") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SourceAdapterError("image_path_invalid", "image path is not a regular file")
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise SourceAdapterError("image_storage_failed", "image could not be removed") from exc
    return True