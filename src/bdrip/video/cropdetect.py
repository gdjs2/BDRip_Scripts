"""Crop inspection command using the same PyAV detector as CRF calibration."""

from __future__ import annotations

import argparse
from pathlib import Path

from loguru import logger

from bdrip.common.time import parse_time

from .crop import detect_crop as inspect_crop
from .probe import EncodingError, inspect_video


def detect_crop(source: Path, *, start: float = 0.0, end: float = 60.0) -> str:
    """Inspect the requested interval and return an exact numeric crop filter."""
    start, end = parse_time(start), parse_time(end)
    if start >= end:
        raise ValueError("Start time must be less than end time.")
    source = source.expanduser().resolve()
    info = inspect_video(source)
    end = min(end, info["duration"])
    if start >= end:
        raise ValueError("Crop detection starts outside the video duration.")
    result = inspect_crop(
        source,
        0,
        [{"start": start, "duration": end - start}],
        info,
        {"limit": 24 / 255, "seconds": end - start},
    )
    logger.info(result["reason"])
    crop = result["crop"] or f"{info['width']}:{info['height']}:0:0"
    return f"crop={crop}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Detect exact video crop bounds with PyAV."
    )
    parser.add_argument("input_file", help="Path to the input video file.")
    parser.add_argument(
        "--start", "-s", help="Start time (default: 00:00:00); HH:MM:SS or seconds."
    )
    parser.add_argument(
        "--end", "-e", help="End time (default: 00:01:00); HH:MM:SS or seconds."
    )
    args = parser.parse_args(argv)
    if (args.start is None) != (args.end is None):
        parser.error("--start and --end must be provided together.")
    try:
        start = parse_time(args.start if args.start is not None else 0)
        end = parse_time(args.end if args.end is not None else 60)
        if start >= end:
            raise ValueError("Start time must be less than end time.")
    except ValueError as exc:
        parser.error(str(exc))
    try:
        crop = detect_crop(Path(args.input_file), start=start, end=end)
    except (OSError, ValueError, EncodingError) as exc:
        logger.error(str(exc))
        return 1
    logger.success(f"[detect cropfilter] Final crop filter: {crop}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
