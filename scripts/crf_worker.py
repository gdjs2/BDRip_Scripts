"""Run one desktop task while excluding competing workers for its queue."""

import argparse
from pathlib import Path
import sys

from PySide6.QtCore import QLockFile


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--workspace", type=Path, required=True)
    args, calibration_args = parser.parse_known_args()
    lock = QLockFile(str(args.workspace / "worker.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        print("Another task is already running in this queue.", file=sys.stderr)
        return 1
    try:
        if __package__:
            from .crf_search import main as calibrate
        else:
            from crf_search import main as calibrate
        return calibrate(calibration_args)
    finally:
        lock.unlock()


if __name__ == "__main__":
    raise SystemExit(main())
