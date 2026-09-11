"""PyAV video inspection and an isolated, JSON-speaking sample encoder.

The encoder never creates an output movie: compressed packet sizes provide the
video byte count. x264's closing summary and x265's frame CSV provide the
quantization averages and frame counts used by the CRF calibration.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import gc
import json
import math
import re
import sys
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Any

import av

from bdrip.common.progress import FrameProgress

from .crop import ExactCrop
from .probe import (
    _COLOR_ATTRIBUTES,
    EncodingError,
    _rate,
    _stream_start,
    _video_stream,
    inspect_video,
)

_LIBRARIES = {"x264": "libx264", "x265": "libx265"}
_X264_SUMMARY = re.compile(r"frame\s+([IPB]):\s*(\d+)\s+Avg QP:\s*(-?\d+(?:\.\d+)?)")
_INVALID_OPTION = re.compile(
    r"error parsing option|unknown option|invalid value for|"
    r"(?:MB rate|frame MB size|DPB size|VBV bitrate|VBV buffer).*level limit",
    re.IGNORECASE,
)


def check_encoder(codec: str, settings: dict[str, Any]) -> dict[str, Any]:
    """Check that this PyAV installation includes the requested encoder."""
    if codec not in _LIBRARIES:
        raise EncodingError("codec must be x264 or x265")
    library = _LIBRARIES[codec]
    try:
        encoder = av.Codec(library, "w")
    except (av.FFmpegError, ValueError) as exc:
        raise EncodingError(
            f"This PyAV installation does not include {library}. "
            "Install a supported PyAV binary wheel with this encoder."
        ) from exc
    formats = [item.name for item in (encoder.video_formats or [])]
    pixel_format = settings.get(
        "pixel_format", "yuv420p" if codec == "x264" else "yuv420p10le"
    )
    if pixel_format not in formats:
        raise EncodingError(
            f"{library} does not support pixel format {pixel_format!r}; "
            f"available formats: {', '.join(formats)}"
        )
    return {
        "library": library,
        "pixel_format": pixel_format,
        "pyav_version": av.__version__,
    }


def _crop_dimensions(crop: str | None, width: int, height: int) -> tuple[int, int]:
    if crop is None:
        return width, height
    match = re.fullmatch(r"(\d+):(\d+):(\d+):(\d+)", crop)
    if not match:
        raise EncodingError("crop must be four integers in width:height:x:y form")
    out_width, out_height, x, y = map(int, match.groups())
    if (
        out_width <= 0
        or out_height <= 0
        or x + out_width > width
        or y + out_height > height
    ):
        raise EncodingError(f"Crop {crop!r} is outside the {width}x{height} source")
    return out_width, out_height


def validate_source_settings(
    source: dict[str, Any],
    codec: str,
    settings: dict[str, Any],
    crop: str | None = None,
) -> tuple[int, int]:
    """Validate settings that would otherwise silently resize or mislabel video."""
    capabilities = check_encoder(codec, settings)
    width, height = _crop_dimensions(crop, source["width"], source["height"])
    pixel_format = av.VideoFormat(capabilities["pixel_format"])
    if (pixel_format.chroma_width(4) < 4 and width % 2) or (
        pixel_format.chroma_height(4) < 4 and height % 2
    ):
        raise EncodingError(
            f"{width}x{height} is incompatible with the chroma subsampling of "
            f"{capabilities['pixel_format']}; choose an explicit crop with even width and height. "
            "Detected dimensions are not rounded automatically."
        )
    if codec == "x264" and str(settings.get("level")) in {"4.1", "41"}:
        macroblocks = math.ceil(width / 16) * math.ceil(height / 16)
        if macroblocks > 8192 or macroblocks * Fraction(source["fps"]) > 245760:
            raise EncodingError(
                f"{width}x{height} at {source['fps']} fps exceeds H.264 level 4.1. "
                "Choose a suitable level; the analyzer does not resize or drop frames."
            )
    return width, height


def _encoder_options(job: dict[str, Any], csv_name: str) -> dict[str, str]:
    codec = job["codec"]
    settings = job["settings"]
    params = {str(key): str(value) for key, value in settings.get("params", {}).items()}
    forbidden = {"crf", "qp", "bitrate", "pass", "stats", "lossless"} & set(params)
    if forbidden:
        raise EncodingError(
            "Custom parameters cannot override the CRF experiment: "
            + ", ".join(sorted(forbidden))
        )
    options = {
        str(key): str(value) for key, value in settings.get("options", {}).items()
    }
    controlled = {
        "crf",
        "preset",
        "tune",
        "profile",
        "level",
        "x264-params",
        "x265-params",
    }
    if set(options) & controlled:
        raise EncodingError(
            "Use the named settings fields for CRF, preset, profile, level and encoder params"
        )
    # PyAV defaults to slice threading, which would force x264's sliced-threads
    # mode and change the experiment's compression behavior. Zero preserves
    # the native encoder preset/tune choice; callers can override it explicitly.
    options.setdefault("thread_type", "0")
    default_preset = "placebo" if codec == "x264" else "slower"
    options.update(
        {"crf": str(job["crf"]), "preset": str(settings.get("preset", default_preset))}
    )
    for key in ("tune", "profile", "level"):
        value = settings.get(key)
        if value is not None:
            if key == "level" and codec == "x265":
                params["level-idc"] = str(value)
            else:
                options[key] = str(value)
    if codec == "x265":
        # This relative name lives inside a private temporary directory, which
        # avoids platform-specific escaping of drive letters and colon paths.
        params.update({"csv": csv_name, "csv-log-level": "1", "log-level": "2"})
    if any(":" in key or "=" in key or ":" in value for key, value in params.items()):
        raise EncodingError(
            "Encoder parameter keys and values cannot contain ':'; use ',' for paired values"
        )
    if params:
        options[f"{codec}-params"] = ":".join(
            f"{key}={value}" for key, value in params.items()
        )
    return options


def parse_x264_summary(
    logs: list[tuple[int, str, str]],
) -> tuple[dict[str, float], dict[str, int]]:
    qp: dict[str, float] = {}
    counts: dict[str, int] = {}
    for _level, name, message in logs:
        if "264" not in name:
            continue
        match = _X264_SUMMARY.search(message)
        if match:
            kind, count, average = match.groups()
            if int(count) > 0:
                counts[kind] = int(count)
                qp[kind] = float(average)
    if not qp:
        raise EncodingError("x264 did not report its final I/P/B average QP statistics")
    return qp, counts


def parse_x265_csv(path: Path) -> tuple[dict[str, float], dict[str, int]]:
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    if not path.is_file():
        raise EncodingError("x265 did not create its requested frame statistics CSV")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, skipinitialspace=True)
        if not reader.fieldnames:
            raise EncodingError("x265 frame statistics CSV is empty")
        reader.fieldnames = [name.strip() for name in reader.fieldnames]
        for row in reader:
            kind = row.get("Type", "").strip().upper().removesuffix("-SLICE")
            if kind not in {"I", "P", "B"}:
                continue
            try:
                value = float(row["QP"])
            except (KeyError, TypeError, ValueError) as exc:
                raise EncodingError(
                    "x265 frame statistics contain an invalid QP value"
                ) from exc
            if not math.isfinite(value):
                raise EncodingError(
                    "x265 frame statistics contain a non-finite QP value"
                )
            totals[kind] = totals.get(kind, 0.0) + value
            counts[kind] = counts.get(kind, 0) + 1
    if not counts:
        raise EncodingError("x265 frame statistics contain no I/P/B QP values")
    return {kind: total / counts[kind] for kind, total in totals.items()}, counts


def _report_logs(logs: list[tuple[int, str, str]]) -> None:
    for _level, name, message in logs:
        sys.stderr.write(f"[{name}] {message.rstrip()}\n")
    sys.stderr.flush()


def _reject_invalid_options(logs: list[tuple[int, str, str]]) -> None:
    invalid = [
        message.strip()
        for _level, _name, message in logs
        if _INVALID_OPTION.search(message)
    ]
    if invalid:
        raise EncodingError("Encoder rejected parameters: " + "; ".join(invalid))


def _encode_frames(
    job: dict[str, Any],
    source: dict[str, Any],
    options: dict[str, str],
    logs: list[tuple[int, str, str]],
) -> dict[str, Any]:
    """Keep codec ownership within this scope so closing logs are captured."""
    encoder = None
    decoder = None
    try:
        with av.open(str(job["input"]), mode="r") as container:
            stream = _video_stream(container, job.get("video_index", 0))
            rate = _rate(stream)
            stream_start = _stream_start(container, stream)
            target_start = stream_start + Fraction(str(job["start"]))
            target_end = target_start + Fraction(str(job["duration"]))
            if not stream.time_base:
                raise EncodingError(
                    "The selected video stream has no timestamp time base"
                )
            if target_start > stream_start:
                container.seek(
                    int(target_start / stream.time_base), stream=stream, backward=True
                )

            width, height = validate_source_settings(
                source, job["codec"], job["settings"], job.get("crop")
            )
            encoder = av.CodecContext.create(_LIBRARIES[job["codec"]], "w")
            encoder.width, encoder.height = width, height
            encoder.pix_fmt = job["settings"].get(
                "pixel_format", "yuv420p" if job["codec"] == "x264" else "yuv420p10le"
            )
            encoder.time_base = stream.time_base
            encoder.framerate = rate
            encoder.sample_aspect_ratio = stream.sample_aspect_ratio or Fraction(1)
            for name in _COLOR_ATTRIBUTES:
                setattr(encoder, name, getattr(stream.codec_context, name))
            encoder.options = options.copy()
            encoder.open()
            if encoder.options:
                raise EncodingError(
                    "Unrecognized encoder options: "
                    + ", ".join(sorted(encoder.options))
                )
            _reject_invalid_options(logs)

            first_time: Fraction | None = None
            final_time: Fraction | None = None
            last_step: Fraction | None = None
            video_bytes = 0
            frames = 0
            packet_count = 0
            crop_filter = ExactCrop(job.get("crop"))
            progress = FrameProgress(job.get("progress_file"))

            def submit(frame: Any, timestamp: Fraction, duration: Fraction) -> None:
                nonlocal first_time, final_time, video_bytes, frames, packet_count
                if first_time is None:
                    first_time = timestamp
                if duration <= 0:
                    raise EncodingError(
                        "Decoded video contains a frame with no positive presentation duration"
                    )
                frame = crop_filter.apply(frame)
                frame = frame.reformat(format=encoder.pix_fmt)
                ticks = (timestamp - first_time) / encoder.time_base
                if ticks.denominator != 1:
                    raise EncodingError(
                        "Decoded frame timestamps cannot be represented exactly in the stream time base"
                    )
                frame.pts = int(ticks)
                frame.time_base = encoder.time_base
                frame.duration = max(1, round(duration / encoder.time_base))
                # Decoded frame types describe the source GOP, not the GOP the
                # current encoder should choose for this independently encoded sample.
                frame.pict_type = av.video.frame.PictureType.NONE
                for packet in encoder.encode(frame):
                    video_bytes += packet.size
                    packet_count += 1
                frames += 1
                final_time = timestamp + duration
                progress.update(
                    frames, float(final_time - first_time) / job["duration"]
                )

            pending = None
            pending_time: Fraction | None = None
            decoder = container.decode(stream)
            previous_time: Fraction | None = None
            reached_end = False
            for frame in decoder:
                if (frame.width, frame.height) != (source["width"], source["height"]):
                    raise EncodingError(
                        "The input changes video dimensions while decoding; "
                        "the analyzer cannot preserve this with one fixed encoder configuration"
                    )
                if frame.pts is None or not frame.time_base:
                    raise EncodingError(
                        "Decoded video frames have no presentation timestamps"
                    )
                timestamp = frame.pts * Fraction(frame.time_base)
                if previous_time is not None and timestamp <= previous_time:
                    raise EncodingError(
                        "Decoded video presentation timestamps are not strictly increasing"
                    )
                previous_time = timestamp
                if timestamp < target_start:
                    continue
                if pending is not None:
                    last_step = timestamp - pending_time
                    submit(pending, pending_time, last_step)
                    pending = None
                if timestamp >= target_end:
                    reached_end = True
                    break
                pending, pending_time = frame, timestamp
            if pending is not None and not reached_end:
                last_duration = (
                    pending.duration * Fraction(pending.time_base)
                    if pending.duration and pending.duration > 0
                    else last_step or Fraction(1, 1) / rate
                )
                submit(pending, pending_time, last_duration)
            if not frames or first_time is None or final_time is None:
                raise EncodingError("The requested sample contains no video frames")
            progress.update(frames, 0.99, flushing=True)
            for packet in encoder.encode(None):
                video_bytes += packet.size
                packet_count += 1
            if packet_count != frames or video_bytes <= 0:
                raise EncodingError(
                    f"Encoder produced {packet_count} packets for {frames} frames; "
                    "the sample measurement is incomplete"
                )
            return {
                "duration": float(final_time - first_time),
                "video_bytes": video_bytes,
                "frames": frames,
                "encoder_options": options,
                "actual_start": float(first_time - stream_start),
                "actual_end": float(final_time - stream_start),
                "width": width,
                "height": height,
            }
    finally:
        if decoder is not None:
            decoder.close()
        # x264 prints its summary in avcodec_free_context. This assignment
        # occurs before the surrounding Capture context is exited.
        encoder = None
        gc.collect()


def encode_sample(job: dict[str, Any]) -> dict[str, Any]:
    """Encode one selected range; intended to run in its own Python process."""
    job = dict(job)
    job["input"] = str(Path(job["input"]).expanduser().resolve())
    job.setdefault("settings", {})
    for name in ("start", "duration", "crf"):
        value = job.get(name)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise EncodingError(f"{name} must be a finite number")
    if job["start"] < 0 or job["duration"] <= 0:
        raise EncodingError(
            "Sample start must be nonnegative and duration must be positive"
        )
    if not 0 <= job["crf"] <= 51:
        raise EncodingError("CRF must be between 0 and 51")
    source = inspect_video(job["input"], job.get("video_index", 0))
    validate_source_settings(source, job["codec"], job["settings"], job.get("crop"))
    previous_level = av.logging.get_level()
    av.logging.set_level(av.logging.INFO)
    logs: list[tuple[int, str, str]] = []
    try:
        with tempfile.TemporaryDirectory(prefix="crf-encode-") as temporary:
            with contextlib.chdir(temporary):
                options = _encoder_options(job, "frames.csv")
                with av.logging.Capture(local=False) as logs:
                    result = _encode_frames(job, source, options, logs)
                _reject_invalid_options(logs)
                if job["codec"] == "x264":
                    qp, counts = parse_x264_summary(logs)
                else:
                    qp, counts = parse_x265_csv(Path("frames.csv"))
                if sum(counts.values()) != result["frames"]:
                    raise EncodingError(
                        "Encoder QP statistics do not account for every encoded video frame"
                    )
                result.update({"qp": qp, "frame_counts": counts})
                return result
    except av.FFmpegError as exc:
        raise EncodingError(f"Sample encoding failed: {exc}") from exc
    finally:
        _report_logs(logs)
        av.logging.set_level(previous_level)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True, type=Path, help="JSON sample job file")
    args = parser.parse_args()
    try:
        job = json.loads(args.job.read_text(encoding="utf-8"))
        result = encode_sample(job)
        print(json.dumps(result, allow_nan=False))
        return 0
    except (EncodingError, ValueError, TypeError, KeyError, OSError) as exc:
        print(f"CRF sample failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
