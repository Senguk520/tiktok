"""Atomic single-file writer for validated live-check evidence."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from live_checks.report import LiveCheckReport, serialize_report


class LiveCheckWriteError(RuntimeError):
    """Raised when one validated report cannot replace its target."""


def atomic_write_report(report: LiveCheckReport, target: Path) -> None:
    selected = Path(target)
    if selected.suffix.lower() != ".json" or not selected.parent.is_dir():
        raise LiveCheckWriteError("live-check target must be a JSON file in an existing directory")
    body = serialize_report(report)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=selected.parent,
            prefix=f".{selected.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, selected)
        temporary = None
    except (OSError, ValueError) as exc:
        raise LiveCheckWriteError("live-check artifact could not be written") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass