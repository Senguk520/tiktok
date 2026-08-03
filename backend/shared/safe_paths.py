"""Constrained filesystem helpers for runtime-owned files.

The application deliberately has no generic file path API.  Every runtime file
must be classified before it is opened or removed:

* SQLite databases are direct children of ``data`` and end in ``.sqlite3``.
* Temporary images are UUID-named files directly under ``temp/images``.
* Configuration files are selected through an explicit category.

All paths are resolved before validation and existing reparse points/symlinks
are rejected.  This keeps an escaped path from becoming safe merely because
its textual spelling starts with the project directory.
"""

from __future__ import annotations

import os
import re
import stat
from enum import StrEnum
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
DATA_DIR: Final[Path] = PROJECT_ROOT / "data"
IMAGE_DIR: Final[Path] = PROJECT_ROOT / "temp" / "images"
COLLECTOR_IMAGE_DIR: Final[Path] = IMAGE_DIR / "collector"
CONFIG_DIR: Final[Path] = PROJECT_ROOT / "config"

MAX_IMAGE_BYTES: Final[int] = 10 * 1024 * 1024
IMAGE_MIME_BY_EXTENSION: Final[dict[str, str]] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
UUID_IMAGE_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\.(?:jpg|jpeg|png|webp|gif)$"
)
SQLITE_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.sqlite3$")


class UnsafePathError(ValueError):
    """Raised when a path does not belong to an explicitly allowed boundary."""


class InvalidImageError(ValueError):
    """Raised when an image name, MIME type, or size is unsafe."""


class ConfigCategory(StrEnum):
    """The only configuration file classes that may be addressed by runtime code."""

    ENV = "env"
    JSON = "json"
    TOML = "toml"
    YAML = "yaml"


_CONFIG_SUFFIXES: Final[dict[ConfigCategory, frozenset[str]]] = {
    ConfigCategory.ENV: frozenset({".env"}),
    ConfigCategory.JSON: frozenset({".json"}),
    ConfigCategory.TOML: frozenset({".toml"}),
    ConfigCategory.YAML: frozenset({".yaml", ".yml"}),
}
_ALLOWED_ENV_NAMES: Final[frozenset[str]] = frozenset({".env", ".env.local"})


def _as_path(value: str | os.PathLike[str] | Path) -> Path:
    if isinstance(value, Path):
        return value
    return Path(value)


def _resolved(value: str | os.PathLike[str] | Path, *, base: Path = PROJECT_ROOT) -> Path:
    path = _as_path(value)
    if not path.is_absolute():
        path = base / path
    try:
        return path.resolve(strict=False)
    except OSError as exc:
        raise UnsafePathError("path cannot be resolved") from exc


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _has_existing_reparse_point(path: Path, root: Path = PROJECT_ROOT) -> bool:
    """Check each existing component without following links.

    ``st_file_attributes`` covers Windows junctions/reparse points while
    ``S_ISLNK`` covers POSIX symlinks and ordinary Windows links.
    """

    if not _is_within(path, root):
        return True
    relative_parts = path.relative_to(root).parts
    current = root
    for part in relative_parts:
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            # A missing suffix cannot contain an existing link.  Its parents
            # have already been inspected.
            break
        except OSError as exc:
            raise UnsafePathError("cannot inspect path component") from exc
        if stat.S_ISLNK(metadata.st_mode):
            return True
        if getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            return True
    return False


def resolve_project_path(value: str | os.PathLike[str] | Path) -> Path:
    """Resolve a path and require it to remain below ``PROJECT_ROOT``.

    The lexical path is inspected before canonicalisation so a symlink that
    happens to point back inside the project is still rejected.
    """

    root = PROJECT_ROOT.resolve(strict=True)
    raw = _as_path(value)
    absolute = raw if raw.is_absolute() else root / raw
    lexical = Path(os.path.abspath(absolute))
    if not _is_within(lexical, root):
        raise UnsafePathError("path escapes project root")
    if _has_existing_reparse_point(lexical, root):
        raise UnsafePathError("symlinks and reparse points are not allowed")
    path = _resolved(absolute, base=root)
    if not _is_within(path, root):
        raise UnsafePathError("path escapes project root")
    return path


def ensure_runtime_directories() -> tuple[Path, Path]:
    """Create only the approved database and image runtime directories.

    No caller receives a generic directory-creation primitive. Existing
    symlinks/reparse points are rejected before and after creation. Collector
    images are isolated below ``temp/images/collector`` while the return shape
    remains compatible with existing callers.
    """

    data_dir = resolve_project_path(DATA_DIR)
    temp_dir = resolve_project_path(IMAGE_DIR.parent)
    for directory in (data_dir, temp_dir):
        directory.mkdir(exist_ok=True)
        resolve_project_path(directory)
    image_dir = resolve_project_path(IMAGE_DIR)
    image_dir.mkdir(exist_ok=True)
    resolve_project_path(image_dir)
    collector_image_dir = resolve_project_path(COLLECTOR_IMAGE_DIR)
    collector_image_dir.mkdir(exist_ok=True)
    resolve_project_path(collector_image_dir)
    return data_dir, image_dir


def resolve_sqlite_path(value: str | os.PathLike[str] | Path) -> Path:
    """Resolve only a direct ``data/*.sqlite3`` runtime database path."""

    path = resolve_project_path(value)
    data_dir = DATA_DIR.resolve(strict=False)
    if path.parent != data_dir or not SQLITE_NAME_RE.fullmatch(path.name):
        raise UnsafePathError("runtime database must be data/<name>.sqlite3")
    if path.name.lower() in {".sqlite3", "..sqlite3"}:
        raise UnsafePathError("invalid database filename")
    return path


def resolve_image_path(value: str | os.PathLike[str] | Path) -> Path:
    """Resolve only a UUID-named image directly under ``temp/images``."""

    path = resolve_project_path(value)
    image_dir = IMAGE_DIR.resolve(strict=False)
    if path.parent != image_dir or not UUID_IMAGE_RE.fullmatch(path.name):
        raise InvalidImageError("image must be UUID.ext under temp/images")
    extension = path.suffix.lower()
    if extension not in IMAGE_MIME_BY_EXTENSION:
        raise InvalidImageError("image extension is not allowed")
    return path


def resolve_collector_image_path(value: str | os.PathLike[str] | Path) -> Path:
    """Resolve one UUID-named Collector image below its dedicated directory."""

    path = resolve_project_path(value)
    image_dir = COLLECTOR_IMAGE_DIR.resolve(strict=False)
    if path.parent != image_dir or not UUID_IMAGE_RE.fullmatch(path.name):
        raise InvalidImageError("collector image must be UUID.ext under temp/images/collector")
    if path.suffix.lower() not in IMAGE_MIME_BY_EXTENSION:
        raise InvalidImageError("collector image extension is not allowed")
    return path


def resolve_runtime_path(value: str | os.PathLike[str] | Path) -> Path:
    """Resolve a runtime path only when it is a database or approved image path."""

    path = resolve_project_path(value)
    try:
        return resolve_sqlite_path(path)
    except UnsafePathError:
        pass
    for resolver in (resolve_image_path, resolve_collector_image_path):
        try:
            return resolver(path)
        except InvalidImageError:
            continue
    raise UnsafePathError("runtime path is not an approved database or image")


def resolve_config_path(
    filename: str | os.PathLike[str] | Path, category: ConfigCategory | str
) -> Path:
    """Resolve a configuration file through an explicit, narrow category.

    Environment files are restricted to project-root ``.env``/``.env.local``.
    Other configuration classes are direct children of ``config`` and cannot
    contain a directory component.
    """

    try:
        kind = ConfigCategory(category)
    except ValueError as exc:
        raise UnsafePathError("unknown configuration category") from exc
    raw = _as_path(filename)
    if raw.is_absolute() or len(raw.parts) != 1 or raw.name in {"", ".", ".."}:
        raise UnsafePathError("configuration filename must be a single name")
    if kind is ConfigCategory.ENV:
        if raw.name not in _ALLOWED_ENV_NAMES:
            raise UnsafePathError("environment filename is not allowed")
        candidate = PROJECT_ROOT / raw.name
    else:
        if raw.suffix.lower() not in _CONFIG_SUFFIXES[kind]:
            raise UnsafePathError("configuration extension does not match category")
        candidate = CONFIG_DIR / raw.name
    return resolve_project_path(candidate)


def validate_image_metadata(
    filename: str | os.PathLike[str] | Path,
    *,
    content_type: str,
    size: int,
) -> Path:
    """Validate upload metadata before any file is opened."""

    path = resolve_image_path(filename)
    expected_mime = IMAGE_MIME_BY_EXTENSION[path.suffix.lower()]
    normalized_mime = content_type.split(";", 1)[0].strip().lower()
    if normalized_mime != expected_mime:
        raise InvalidImageError("MIME type does not match image extension")
    if size < 0 or size > MAX_IMAGE_BYTES:
        raise InvalidImageError("image exceeds the maximum size")
    return path


def _sniff_image_mime(header: bytes) -> str | None:
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_image_bytes(
    filename: str,
    *,
    content_type: str,
    content: bytes,
) -> str:
    """Validate an in-memory image before sending it to an upstream API.

    The original client path is never accepted: callers provide one basename,
    and only its validated basename is forwarded as multipart metadata.
    """

    raw = Path(filename)
    if (
        not filename
        or raw.is_absolute()
        or len(raw.parts) != 1
        or raw.name in {"", ".", ".."}
    ):
        raise InvalidImageError("image filename must be a single basename")
    extension = raw.suffix.lower()
    expected_mime = IMAGE_MIME_BY_EXTENSION.get(extension)
    normalized_mime = content_type.split(";", 1)[0].strip().lower()
    if expected_mime is None or normalized_mime != expected_mime:
        raise InvalidImageError("image MIME type does not match an allowed extension")
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise InvalidImageError("image is empty or exceeds the maximum size")
    if _sniff_image_mime(content[:16]) != expected_mime:
        raise InvalidImageError("image content does not match extension")
    return raw.name


def validate_image_file(
    filename: str | os.PathLike[str] | Path,
    *,
    content_type: str | None = None,
) -> Path:
    """Validate an already-written image using size and magic bytes."""

    path = resolve_image_path(filename)
    try:
        metadata = path.stat()
    except FileNotFoundError as exc:
        raise InvalidImageError("image does not exist") from exc
    except OSError as exc:
        raise InvalidImageError("image cannot be inspected") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_IMAGE_BYTES:
        raise InvalidImageError("image is not a regular file or is too large")
    try:
        with path.open("rb") as image_file:
            mime = _sniff_image_mime(image_file.read(16))
    except OSError as exc:
        raise InvalidImageError("image cannot be read") from exc
    expected_mime = IMAGE_MIME_BY_EXTENSION[path.suffix.lower()]
    if mime != expected_mime:
        raise InvalidImageError("image content does not match extension")
    if content_type is not None and content_type.split(";", 1)[0].strip().lower() != mime:
        raise InvalidImageError("declared MIME type does not match image content")
    return path


def safe_unlink_image(value: str | os.PathLike[str] | Path) -> bool:
    """Delete exactly one registered-looking image file, never its directory."""

    path = resolve_image_path(value)
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise UnsafePathError("image cannot be inspected") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise UnsafePathError("only a regular image file can be removed")
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


# Explicit aliases keep call sites descriptive without exposing a generic unlink.
safe_delete_image = safe_unlink_image
validate_image_upload = validate_image_metadata