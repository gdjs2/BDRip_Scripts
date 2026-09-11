"""Deadline-aware Python workers for native video inspection and encoding."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


class BudgetExpired(RuntimeError):
    """The available wall-clock budget ended before an operation completed."""


def _check_timeout(timeout: float | None) -> None:
    if timeout is None:
        return
    if not math.isfinite(timeout):
        raise ValueError("Process timeout must be finite or None")
    if timeout <= 0:
        raise BudgetExpired("The processing time budget has expired")


def _kill_and_reap(process: subprocess.Popen[str]) -> None:
    """Kill immediately, then drain pipes and collect the child exit status."""
    try:
        process.kill()
    except ProcessLookupError:
        pass  # The child may have exited between the timeout and kill.
    try:
        process.communicate()
    finally:
        # Ensure reaping even when a pipe read fails during cleanup. There is
        # no graceful-shutdown delay: the kill has already been issued.
        if process.poll() is None:
            process.wait()


def run_process(
    command: list[str], *, timeout: float | None, stderr: Any
) -> str:
    """Return UTF-8 stdout, terminating and reaping interrupted child work."""
    _check_timeout(timeout)
    deadline = None if timeout is None else time.monotonic() + timeout
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=stderr,
        text=True, encoding="utf-8", errors="replace",
    )
    try:
        available = None if deadline is None else deadline - time.monotonic()
        if available is not None and available <= 0:
            raise subprocess.TimeoutExpired(command, timeout)
        stdout, _unused_stderr = process.communicate(timeout=available)
    except subprocess.TimeoutExpired as exc:
        _kill_and_reap(process)
        raise BudgetExpired("The processing time budget expired while a Python worker was running") from exc
    except BaseException:
        _kill_and_reap(process)
        raise
    if process.returncode:
        raise RuntimeError(f"Python worker exited with code {process.returncode}")
    return stdout


def _stderr_tail(handle: Any) -> str:
    handle.flush()
    handle.seek(0, 2)
    size = handle.tell()
    handle.seek(max(0, size - 8192))
    return "\n".join(handle.read().decode("utf-8", errors="replace").splitlines()[-20:]).strip()


def run_probe(operation: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    """Inspect a video or detect its crop in a process with a hard deadline."""
    _check_timeout(timeout)
    if operation not in {"inspect", "crop"}:
        raise ValueError(f"Unknown video probe operation: {operation}")
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryDirectory(prefix="crf-probe-") as temporary:
        directory = Path(temporary)
        job_path = directory / "job.json"
        job_path.write_text(
            json.dumps({"operation": operation, "payload": payload}, ensure_ascii=False, allow_nan=False),
            encoding="utf-8",
        )
        with (directory / "stderr.log").open("w+b") as stderr:
            try:
                stdout = run_process(
                    [sys.executable, str(Path(__file__).resolve()), "--job", str(job_path)],
                    timeout=deadline - time.monotonic(), stderr=stderr,
                )
                try:
                    result = json.loads(stdout)
                except (ValueError, TypeError) as exc:
                    raise RuntimeError("Video probe returned invalid JSON") from exc
                if not isinstance(result, dict):
                    raise RuntimeError("Video probe returned a JSON value instead of an object")
                return result
            except BudgetExpired:
                raise
            except RuntimeError as exc:
                tail = _stderr_tail(stderr)
                if tail:
                    raise RuntimeError(f"{exc}\n{tail}") from exc
                raise


def _probe(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    # Import PyAV only inside the worker. Parent orchestration and timeout
    # tests do not need to load native codecs merely to manage a process.
    if operation == "inspect":
        if __package__:
            from .crf_encode import inspect_video
        else:
            from crf_encode import inspect_video
        return inspect_video(payload["input"], payload.get("video_index", 0))
    if operation == "crop":
        if __package__:
            from .crf_crop import detect_crop
        else:
            from crf_crop import detect_crop
        return detect_crop(
            payload["input"], payload.get("video_index", 0), payload["samples"],
            payload["source"], payload["settings"],
        )
    raise ValueError(f"Unknown video probe operation: {operation}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True, type=Path, help="JSON video probe job")
    args = parser.parse_args()
    try:
        job = json.loads(args.job.read_text(encoding="utf-8"))
        result = _probe(job["operation"], job["payload"])
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception as exc:
        print(f"Video probe failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
