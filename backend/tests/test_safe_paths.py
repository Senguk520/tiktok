from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from shared.safe_paths import (
    IMAGE_DIR,
    InvalidImageError,
    UnsafePathError,
    resolve_image_path,
    resolve_project_path,
    resolve_sqlite_path,
    safe_unlink_image,
    validate_image_file,
    validate_image_metadata,
)


def test_rejects_project_and_database_path_escape() -> None:
    with pytest.raises(UnsafePathError):
        resolve_project_path(Path("..") / "outside.txt")
    with pytest.raises(UnsafePathError):
        resolve_sqlite_path(Path("temp") / "not-a-database.sqlite3")


def test_requires_uuid_image_name_and_matching_metadata() -> None:
    image = IMAGE_DIR / f"{uuid4()}.png"
    assert resolve_image_path(image) == image.resolve()
    assert validate_image_metadata(image, content_type="image/png", size=128) == image.resolve()
    with pytest.raises(InvalidImageError):
        resolve_image_path(IMAGE_DIR / "product.png")
    with pytest.raises(InvalidImageError):
        validate_image_metadata(image, content_type="image/svg+xml", size=128)


def test_validates_magic_bytes_and_deletes_only_one_file() -> None:
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    image = IMAGE_DIR / f"{uuid4()}.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"safe-test-payload")
    try:
        assert validate_image_file(image, content_type="image/png") == image.resolve()
        assert safe_unlink_image(image) is True
        assert not image.exists()
        assert IMAGE_DIR.is_dir()
        assert safe_unlink_image(image) is False
    finally:
        if image.exists():
            image.unlink()


def test_rejects_symlink_component_even_when_target_is_inside_project() -> None:
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    link = IMAGE_DIR.parent / f"link-{uuid4()}"
    try:
        try:
            os.symlink(IMAGE_DIR, link, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlink creation unavailable: {exc}")
        candidate = link / f"{uuid4()}.png"
        with pytest.raises(UnsafePathError):
            resolve_project_path(candidate)
    finally:
        try:
            link.unlink()
        except FileNotFoundError:
            pass