"""Console progress shared by release preparation and torrent generation."""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.progress import Progress
from rich.text import Text

CONSOLE = Console(stderr=True)


def status_text(
    label: str,
    detail: str,
    current: int | None = None,
    total: int | None = None,
    step: int | None = None,
    total_steps: int = 7,
) -> Text:
    progress = (
        f" ({current}/{total})" if current is not None and total is not None else ""
    )
    stage = f" - step {step}/{total_steps}" if step is not None else ""
    return Text(f"{label} - {detail}{progress}{stage}")


def update_status(
    status: Any,
    label: str,
    detail: str,
    current: int | None = None,
    total: int | None = None,
    step: int | None = None,
    total_steps: int = 7,
) -> None:
    status.update(status_text(label, detail, current, total, step, total_steps))


class ProgressStatus:
    """Expose a Rich Progress task through the same update API as Rich Status."""

    def __init__(self, progress: Progress, task_id: int) -> None:
        self.progress = progress
        self.task_id = task_id

    def update(self, renderable: Text) -> None:
        self.progress.update(self.task_id, description=renderable.plain, refresh=True)
