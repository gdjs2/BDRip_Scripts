"""Atomic JSON persistence and deterministic filesystem helpers."""

from __future__ import annotations

import json
import re
import tempfile
import time
from pathlib import Path

# Windows readers and antivirus scanners can briefly deny rename/delete access.
# Retry for at most 0.71 seconds; permanent permission errors still surface.
_REPLACE_RETRY_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.16, 0.2, 0.2)


def _replace_with_retry(source: Path, destination: Path) -> None:
    for attempt in range(len(_REPLACE_RETRY_DELAYS) + 1):
        try:
            source.replace(destination)
            return
        except OSError as exc:
            # ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION.
            # Inspect winerror rather than errno: a POSIX permission failure
            # must not be mistaken for a transient Windows sharing conflict.
            if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == len(
                _REPLACE_RETRY_DELAYS
            ):
                raise
            time.sleep(_REPLACE_RETRY_DELAYS[attempt])


def write_json(path: Path, data: dict) -> None:
    """Publish a complete JSON snapshot, tolerating brief Windows file locks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".bdrip-",
            suffix=".json",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
        # Close the temporary file before attempting a Windows rename.
        _replace_with_retry(temporary, path)
    except BaseException:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # Cleanup may hit the same lock; retain the original failure.
                pass
        raise


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def natural_key(path: Path) -> list[object]:
    return [
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", path.name)
    ]


def is_visible_file(path: Path) -> bool:
    return path.is_file() and not path.name.startswith(".")
