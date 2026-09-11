"""Persistent, sequential CRF tasks driven by Qt's asynchronous process events."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import time
import uuid

from PySide6.QtCore import QIODevice, QLockFile, QObject, QProcess, QProcessEnvironment, QTimer, Signal

if __package__:
    from .crf_config import validate_config
    from .crf_model import METHOD
    from .crf_progress import read_json, write_json
else:
    from crf_config import validate_config
    from crf_model import METHOD
    from crf_progress import read_json, write_json


STATES = {"queued", "running", "cancelling", "completed", "failed", "cancelled", "interrupted"}


@dataclass
class Task:
    id: str
    video: str
    codec: str
    config: dict
    created: str
    state: str = "queued"
    progress: dict = field(default_factory=dict)
    attempts: int = 0
    started: float | None = None
    finished: float | None = None
    error: str = ""
    method: str = METHOD

    @property
    def folder(self) -> str:
        stem = re.sub(r"[^\w.-]+", "_", Path(self.video).stem)[:60].strip(".") or "video"
        return f"{stem}-{self.id}"

    @property
    def percent(self) -> int:
        if self.state == "completed":
            return 100
        total = self.progress.get("total", 0)
        if not total:
            return 0
        fraction = self.progress.get("sample_fraction", 0) if self.state == "running" else 0
        return min(99, max(0, int(100 * (self.progress.get("completed", 0) + fraction) / total)))


class TaskQueue(QObject):
    changed = Signal()
    problem = Signal(str)

    def __init__(self, workspace: Path, parent=None):
        super().__init__(parent)
        self.workspace = workspace.expanduser().resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.lock = QLockFile(str(self.workspace / "queue.lock"))
        self.lock.setStaleLockTime(0)
        if not self.lock.tryLock(0):
            raise ValueError("This queue is already open in another window. Choose a different --workspace.")
        self.tasks: list[Task] = []
        self.active: Task | None = None
        self.process: QProcess | None = None
        self.enabled = False
        try:
            # A worker may outlive an abnormally closed GUI. Do not run a second
            # worker over its reports or cache while it still holds this lock.
            worker_lock = QLockFile(str(self.workspace / "worker.lock"))
            worker_lock.setStaleLockTime(0)
            if not worker_lock.tryLock(0):
                raise ValueError("A background task in this queue is still running. Reopen the queue after it finishes.")
            worker_lock.unlock()
            self._load()
        except BaseException:
            self.lock.unlock()
            raise
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.poll)
        self.timer.start()

    def output(self, task: Task) -> Path:
        return self.workspace / task.folder

    def _load(self) -> None:
        path = self.workspace / "queue.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["schema_version"] not in {1, 2} or not isinstance(data["tasks"], list):
                raise ValueError("Unsupported queue format")
            for item in data["tasks"]:
                task = Task(**{**item, "method": item.get("method", "sweep")})
                if (not re.fullmatch(r"[0-9a-f]{32}", task.id) or task.state not in STATES
                        or task.codec not in {"both", "x264", "x265"} or not isinstance(task.video, str)
                        or not isinstance(task.progress, dict) or task.method not in {METHOD, "sweep"}):
                    raise ValueError("Invalid saved task")
                normalized_config = validate_config(task.config)
                # Upgrade waiting work, while keeping historical settings and
                # artifacts associated with measurements from the old sweep.
                if task.method == "sweep" and task.state == "queued":
                    if task.attempts == 0:
                        task.method = METHOD
                    else:
                        task.state = "interrupted"
                        task.error = "Previous sweep retained; retry to create a two-point task"
                if task.method == METHOD:
                    task.config = normalized_config
                if task.state in {"running", "cancelling"}:
                    task.progress = read_json(self.output(task) / "progress.json")
                    state = task.progress.get("state")
                    task.state = {"complete": "completed", "failed": "failed",
                                  "interrupted": "cancelled"}.get(state, "interrupted")
                    task.error = ("" if task.state == "completed" else
                                  task.progress.get("message", "Previous run was interrupted"))
                    task.finished = (task.started or time.time()) + max(0, task.progress.get("elapsed_seconds", 0))
                self.tasks.append(task)
            if len({task.id for task in self.tasks}) != len(self.tasks):
                raise ValueError("Duplicate saved task IDs")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"Cannot read {path}: {exc}. The existing queue has been left intact.") from exc

    def save(self) -> None:
        write_json(self.workspace / "queue.json", {"schema_version": 2,
                   "tasks": [asdict(task) for task in self.tasks]})

    def add(self, videos: list[str], config: dict, codec: str) -> list[Task]:
        config = validate_config(config)
        if codec not in {"both", "x264", "x265"}:
            raise ValueError("Select x264, x265, or both")
        paths = [Path(video).expanduser().resolve() for video in videos]
        for path in paths:
            if not path.is_file():
                raise ValueError(f"Video does not exist: {path}")
        added = []
        for path in paths:
            task = Task(uuid.uuid4().hex, str(path), codec, validate_config(config),
                        datetime.now(timezone.utc).isoformat())
            write_json(self.output(task) / "config.json", task.config)
            added.append(task)
        self.tasks.extend(added)
        self.save()
        self.changed.emit()
        if self.enabled:
            QTimer.singleShot(0, self._advance)
        return added

    def start(self) -> None:
        self.enabled = True
        self._advance()

    def pause(self) -> None:
        """Finish the active task, leaving the remaining tasks queued."""
        self.enabled = False
        self.changed.emit()

    def cancel(self, task: Task) -> None:
        if task is self.active:
            (self.output(task) / "cancel.request").touch()
            task.state = "cancelling"
        elif task.state == "queued":
            task.state = "cancelled"
            task.finished = time.time()
        else:
            return
        self.save()
        self.changed.emit()

    def retry(self, task: Task) -> Task | None:
        if task.state not in {"failed", "cancelled", "interrupted"}:
            return
        if task.method != METHOD:
            return self.add([task.video], task.config, task.codec)[0]
        task.state, task.error, task.progress = "queued", "", {}
        task.started = task.finished = None
        # Append retries after tasks that were already waiting.
        self.tasks.remove(task)
        self.tasks.append(task)
        self.save()
        self.changed.emit()
        if self.enabled:
            QTimer.singleShot(0, self._advance)
        return task

    def remove(self, task: Task) -> None:
        if task is self.active:
            return
        self.tasks.remove(task)
        self.save()
        self.changed.emit()  # Output folders are deliberately retained.

    def _advance(self) -> None:
        if self.active or not self.enabled:
            return
        task = next((task for task in self.tasks if task.state == "queued"), None)
        if task is None:
            self.enabled = False
            self.changed.emit()
            return
        self.active = task
        task.state, task.error, task.progress = "running", "", {}
        task.attempts += 1
        task.started, task.finished = time.time(), None
        output = self.output(task)
        try:
            output.mkdir(parents=True, exist_ok=True)
            (output / "cancel.request").unlink(missing_ok=True)
            (output / "progress.json").unlink(missing_ok=True)
            write_json(output / "config.json", task.config)
            with (output / "task.log").open("a", encoding="utf-8") as log:
                log.write(f"\n--- Attempt {task.attempts} · {datetime.now(timezone.utc).isoformat()} ---\n")
            self.save()
            process = QProcess(self)
            self.process = process
            process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
            process.setStandardOutputFile(str(output / "task.log"), QIODevice.OpenModeFlag.Append)
            environment = QProcessEnvironment.systemEnvironment()
            for key, value in {"PYTHONUNBUFFERED": "1", "NO_COLOR": "1", "TERM": "dumb",
                               "MPLBACKEND": "Agg", "MPLCONFIGDIR": str(self.workspace / ".matplotlib")}.items():
                environment.insert(key, value)
            process.setProcessEnvironment(environment)
            process.setWorkingDirectory(str(self.workspace))
            process.finished.connect(lambda code, status: self._finished(process, code, status))
            process.errorOccurred.connect(lambda error: self._process_error(process, error))
            process.started.connect(process.closeWriteChannel)
            arguments = [str(Path(__file__).with_name("crf_worker.py")), "--workspace", str(self.workspace),
                         task.video, "--config", str(output / "config.json"), "--codec", task.codec,
                         "--output-dir", str(output), "--progress-file", str(output / "progress.json"),
                         "--cancel-file", str(output / "cancel.request")]
            process.start(sys.executable, ["-u", *arguments])
            self.changed.emit()
        except OSError as exc:
            self._finish_task("failed", str(exc))

    def _process_error(self, process: QProcess, error) -> None:
        if process is self.process and error == QProcess.ProcessError.FailedToStart:
            self._finish_task("failed", f"Cannot start Python worker: {process.errorString()}")

    def _finished(self, process: QProcess, code: int, status) -> None:
        if process is not self.process or not self.active:
            return
        self.poll()
        if (status == QProcess.ExitStatus.NormalExit and code == 0
                and self.active.progress.get("state") == "complete"):
            state, error = "completed", ""
        elif self.active.state == "cancelling" or code == 130:
            state, error = "cancelled", "Cancelled; completed measurements are available for retry"
        else:
            state = "failed"
            error = (self.active.progress.get("message", "Task failed")
                     if self.active.progress.get("state") == "failed"
                     else f"Worker exited with code {code}; see task.log")
        self._finish_task(state, error)

    def _finish_task(self, state: str, error: str) -> None:
        self.active.state, self.active.error = state, error
        self.active.finished = time.time()
        self.active = None
        if self.process:
            self.process.deleteLater()
            self.process = None
        try:
            self.save()
        except OSError as exc:
            self.enabled = False
            self.problem.emit(f"Cannot save the queue: {exc}")
        self.changed.emit()
        QTimer.singleShot(0, self._advance)

    def poll(self) -> None:
        if self.active:
            progress = read_json(self.output(self.active) / "progress.json")
            if progress:
                self.active.progress = progress
            self.changed.emit()

    def close(self) -> None:
        if self.active:
            raise RuntimeError("Cancel the active task and wait for its finished signal before closing")
        self.timer.stop()
        self.lock.unlock()
