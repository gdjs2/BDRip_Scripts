"""Full-file two-pass PyAV encoding with progress and retained native logs."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import av
from loguru import logger
from tqdm import tqdm

from bdrip.common.files import read_json, write_json

from .encode import validate_source_settings
from .probe import EncodingError, inspect_video


def _run_pass(job: dict, workspace: Path, log_file: Path, duration: float) -> dict:
    request = workspace / "job.json"
    progress_file = workspace / "progress.json"
    progress_file.unlink(missing_ok=True)
    write_json(request, {**job, "progress_file": str(progress_file)})
    description = f"{job['codec']} pass {job['pass']} ({job['bitrate']} kbps)"
    with log_file.open("a", encoding="utf-8") as log:
        log.write(f"\n--- {description} ---\n{json.dumps(job, ensure_ascii=False)}\n")
        log.flush()
        process = subprocess.Popen(
            [sys.executable, "-m", "bdrip.video.transcode", "--job", str(request)],
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=log,
            text=True,
            encoding="utf-8",
        )
        try:
            with tqdm(total=duration, unit="s", desc=description) as progress:
                completed = 0.0
                while True:
                    try:
                        stdout, _ = process.communicate(timeout=0.25)
                        break
                    except subprocess.TimeoutExpired:
                        fraction = read_json(progress_file).get("fraction", 0.0)
                        current = duration * min(0.99, max(0.0, fraction))
                        if current > completed:
                            progress.update(current - completed)
                            completed = current
                if not process.returncode:
                    progress.update(duration - completed)
        except BaseException:
            process.kill()
            process.communicate()
            raise
    if process.returncode:
        raise EncodingError(f"{description} failed; see {log_file}")
    try:
        result = json.loads(stdout)
        if (
            result["frames"] <= 0
            or result["video_bytes"] <= 0
            or result["duration"] <= 0
        ):
            raise ValueError("Incomplete encode")
        return result
    except (ValueError, KeyError, TypeError) as exc:
        raise EncodingError(f"Invalid encoder result; see {log_file}") from exc


def encode_two_pass(
    source: Path,
    destination: Path,
    *,
    x264_bitrate: int | None = None,
    x265_bitrate: int | None = None,
    crop_filter: str | None = None,
) -> list[Path]:
    """Encode video only, completing x264's two passes before starting x265.

    Rates are kbps. Both passes use the existing veryslow preset, native
    two-pass statistics, source timing/color, and exact numeric crop bounds.
    """
    bitrates = {"x264": x264_bitrate, "x265": x265_bitrate}
    if all(value is None for value in bitrates.values()):
        raise ValueError(
            "At least one of --x264_bitrate or --x265_bitrate must be specified."
        )
    for codec, value in bitrates.items():
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            raise ValueError(f"{codec}_bitrate must be a positive integer.")
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    info = inspect_video(source)
    if destination.exists() and not destination.is_dir():
        raise ValueError(f"Destination must be a directory: {destination}")
    crop = crop_filter.removeprefix("crop=") if crop_filter else None
    formats = {}
    for codec, rate in bitrates.items():
        if rate is None:
            continue
        formats[codec] = info["pixel_format"]
        supported = {item.name for item in av.Codec(f"lib{codec}", "w").video_formats}
        if formats[codec] not in supported:
            formats[codec] = "yuv420p"
        validate_source_settings(info, codec, {"pixel_format": formats[codec]}, crop)
        output = destination / f"{source.stem}.{codec}.{rate}k.mkv"
        if output.resolve() == source or (output.exists() and output.samefile(source)):
            raise ValueError("Encoded output cannot overwrite the source video")
    logs = destination / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    logger.info(f"Source: {source}; preset: veryslow; crop: {crop}")
    outputs = []
    for codec, rate in bitrates.items():
        if rate is None:
            continue
        basename = f"{source.stem}.{codec}.{rate}k"
        output = destination / f"{basename}.mkv"
        log_file = logs / f"{basename}.encode.log"
        with tempfile.TemporaryDirectory(prefix="bdrip-passes-") as temporary:
            workspace = Path(temporary)
            # Encode to a sibling temporary file so failure/cancellation never
            # replaces an earlier complete movie, even when rerunning a job.
            with tempfile.NamedTemporaryFile(
                dir=destination, prefix=".bdrip-", suffix=".mkv", delete=False
            ) as handle:
                partial = Path(handle.name)
            job = {
                "input": str(source),
                "codec": codec,
                "bitrate": rate,
                "crop": crop,
                "pixel_format": formats[codec],
                "preset": "veryslow",
                "output": str(partial),
            }
            try:
                first = _run_pass(
                    {**job, "pass": 1}, workspace, log_file, info["duration"]
                )
                stats = workspace / "pass.stats"
                if not stats.is_file() or not stats.stat().st_size:
                    raise EncodingError(
                        f"{codec} did not produce first-pass statistics; see {log_file}"
                    )
                second = _run_pass(
                    {**job, "pass": 2}, workspace, log_file, info["duration"]
                )
                if (
                    first["frames"] != second["frames"]
                    or first["duration"] != second["duration"]
                ):
                    raise EncodingError(
                        f"{codec} passes did not encode the same video frames; see {log_file}"
                    )
                if not partial.stat().st_size:
                    raise EncodingError(
                        f"{codec} produced an empty movie; see {log_file}"
                    )
                partial.replace(output)
                outputs.append(output)
            finally:
                partial.unlink(missing_ok=True)
    return outputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Two-pass video encoding with PyAV.")
    parser.add_argument("input_file", type=str, help="Path to the input video file.")
    parser.add_argument(
        "dest_dir", type=str, help="Destination directory for the output files."
    )
    parser.add_argument(
        "--crop_filter",
        "-c",
        type=str,
        help="Exact numeric crop (e.g., 'crop=1280:720:0:0' or '1280:720:0:0').",
    )
    parser.add_argument(
        "--x264_bitrate",
        "-4",
        type=int,
        help="Target bitrate for the output video in x264.",
    )
    parser.add_argument(
        "--x265_bitrate",
        "-5",
        type=int,
        help="Target bitrate for the output video in x265.",
    )
    args = parser.parse_args(argv)
    try:
        outputs = encode_two_pass(
            Path(args.input_file),
            Path(args.dest_dir),
            x264_bitrate=args.x264_bitrate,
            x265_bitrate=args.x265_bitrate,
            crop_filter=args.crop_filter,
        )
    except (OSError, ValueError, EncodingError) as exc:
        logger.error(str(exc))
        return 1
    except KeyboardInterrupt:
        logger.warning("Encoding cancelled; completed outputs and logs were retained.")
        return 130
    for path in outputs:
        logger.success(f"Encoded output: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
