"""Database-first, integrity-checked access to one Collector-owned image."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from collector_app.db.models import ImageRecord
from collector_app.images import inspect_image
from collector_app.sources import SourceAdapterError
from shared.safe_paths import InvalidImageError, UnsafePathError, resolve_collector_image_path

_MAX_IMAGE_BYTES = 5 * 1024 * 1024


class CollectorImageAccessError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class CollectorImageFile:
    image_record_id: str
    content_type: str
    content: bytes


async def read_registered_image(
    session: AsyncSession,
    *,
    image_record_id: str,
) -> CollectorImageFile:
    record = await session.get(ImageRecord, image_record_id)
    if record is None:
        raise LookupError("collector image record was not found")
    if not record.ready or record.deleted_at is not None or not record.relative_path:
        raise CollectorImageAccessError("image_unavailable")
    try:
        path = resolve_collector_image_path(record.relative_path)
        content = await asyncio.to_thread(_read_regular_file, path)
        content_type, width, height = inspect_image(content)
    except FileNotFoundError as exc:
        raise CollectorImageAccessError("image_unavailable") from exc
    except (InvalidImageError, UnsafePathError, SourceAdapterError, OSError) as exc:
        raise CollectorImageAccessError("image_integrity_failed") from exc
    digest = hashlib.sha256(content).hexdigest()
    if (
        not hmac.compare_digest(digest, record.sha256)
        or len(content) != record.byte_size
        or content_type != record.content_type
        or width != record.width
        or height != record.height
    ):
        raise CollectorImageAccessError("image_integrity_failed")
    return CollectorImageFile(
        image_record_id=record.id,
        content_type=content_type,
        content=content,
    )


def _read_regular_file(path: Path) -> bytes:
    metadata = os.lstat(path)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > _MAX_IMAGE_BYTES
    ):
        raise OSError("collector image is not a bounded regular file")
    content = path.read_bytes()
    if len(content) != metadata.st_size:
        raise OSError("collector image changed while being read")
    return content


__all__ = [
    "CollectorImageAccessError",
    "CollectorImageFile",
    "read_registered_image",
]