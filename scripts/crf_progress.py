"""Small, atomic progress files shared by calibration and its desktop queue."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=".crf-", suffix=".json", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(data, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
        except BaseException:
            handle.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


class Progress:
    def __init__(self, path: Path | None = None, cancel_file: Path | None = None):
        self.path = path.expanduser().resolve() if path else None
        self.cancel_file = cancel_file.expanduser().resolve() if cancel_file else None
        self.started = time.monotonic()
        self.data = {"state": "running", "stage": "starting", "message": "Starting",
                     "completed": 0, "total": 0, "sample_fraction": 0.0}

    @property
    def enabled(self) -> bool:
        return self.path is not None or self.cancel_file is not None

    @property
    def sample_path(self) -> Path | None:
        return self.path.with_name("sample-progress.json") if self.path else None

    def check_cancelled(self) -> None:
        if self.cancel_file and self.cancel_file.exists():
            raise KeyboardInterrupt

    def update(self, *, check: bool = True, **values) -> None:
        if check:
            self.check_cancelled()
        self.data.update(values)
        self.data["elapsed_seconds"] = time.monotonic() - self.started
        if self.path:
            write_json(self.path, self.data)


class FrameProgress:
    """Throttle frame notifications; the parent calculates whole-task progress."""

    def __init__(self, path: str | None):
        self.path = Path(path) if path else None
        self.last_update = 0.0

    def update(self, frames: int, fraction: float, *, flushing: bool = False) -> None:
        if self.path and (flushing or time.monotonic() - self.last_update >= 0.5):
            write_json(self.path, {"frames": frames, "fraction": min(0.99, max(0.0, fraction)),
                                   "flushing": flushing})
            self.last_update = time.monotonic()
