"""Conservative black-margin detection with FFmpeg's bbox filter through PyAV.

The bundled PyAV wheel includes bbox but not cropdetect. bbox detects every
pixel above a raw-luminance threshold and exposes exact bounds as frame
metadata. Their exact union is used without rounding dimensions or offsets.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path
from typing import Any

import av

try:
    from .crf_encode import EncodingError, _stream_start, _video_stream
except ImportError:
    from crf_encode import EncodingError, _stream_start, _video_stream


Bounds = tuple[int, int, int, int]  # inclusive x1, y1, x2, y2
_BOUND_KEYS = ("x1", "y1", "x2", "y2")
_DEFAULT_LIMIT = 24 / 255


def _union(first: Bounds | None, second: Bounds) -> Bounds:
    if first is None:
        return second
    return (
        min(first[0], second[0]), min(first[1], second[1]),
        max(first[2], second[2]), max(first[3], second[3]),
    )


def _bounds_dict(bounds: Bounds | None) -> dict[str, int] | None:
    return dict(zip(_BOUND_KEYS, bounds)) if bounds is not None else None


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EncodingError(f"{name} must be a finite number")
    value = float(value)
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise EncodingError(f"{name} must be {'positive' if positive else 'nonnegative'} and finite")
    return value


def _windows(
    samples: list[dict[str, Any]], video_duration: float, seconds: float
) -> list[dict[str, Any]]:
    """Center a brief detection window in each selected sample."""
    result: dict[tuple[float, float], dict[str, Any]] = {}
    for sample in samples:
        start = _number(sample["start"], "Sample start")
        duration = _number(sample["duration"], "Sample duration", positive=True)
        duration = min(duration, max(0.0, video_duration - start))
        if duration <= 0:
            continue
        length = min(duration, seconds)
        center_start = start + (duration - length) / 2
        key = (center_start, length)
        kind = str(sample.get("kind", "representative"))
        if key not in result:
            result[key] = {"start": center_start, "duration": length, "kinds": []}
        if kind not in result[key]["kinds"]:
            result[key]["kinds"].append(kind)
    return sorted(result.values(), key=lambda item: item["start"])


class _LumaBounds:
    """One bbox graph, preserving decoded planar YUV/gray sample values."""

    def __init__(self, limit: float):
        self.limit = limit
        self.graph = self.input = self.output = None
        self.format: str | None = None
        self.depth: int | None = None
        self.threshold: int | None = None

    def read(self, frame: Any) -> Bounds | None:
        components = frame.format.components
        if not components or not components[0].is_luma:
            raise ValueError("The decoded pixel format has no native luminance plane")
        depth = components[0].bits
        if not 8 <= depth <= 16:
            raise ValueError(f"Unsupported {depth}-bit luminance format")
        if self.graph is None:
            self.format, self.depth = frame.format.name, depth
            self.threshold = math.floor(self.limit * ((1 << depth) - 1))
            self.graph = av.filter.Graph()
            self.input = self.graph.add_buffer(template=frame)
            bbox = self.graph.add("bbox", f"min_val={self.threshold}")
            self.output = self.graph.add("buffersink")
            self.input.link_to(bbox)
            bbox.link_to(self.output)
            self.graph.configure()
        elif frame.format.name != self.format:
            raise ValueError("The decoded pixel format changes during crop detection")
        if any(key.startswith("lavfi.bbox.") for key in frame.metadata):
            raise ValueError("Input frames already carry ambiguous bounding-box metadata")
        self.input.push(frame)
        measured = self.output.pull()
        if measured.format.components[0].bits != depth:
            raise ValueError("The bounding-box filter cannot preserve source luminance bit depth")
        metadata = measured.metadata
        values = [metadata.get(f"lavfi.bbox.{key}") for key in _BOUND_KEYS]
        if all(value is None for value in values):
            return None  # Native bbox writes no bounds for an entirely dark frame.
        try:
            x1, y1, x2, y2 = map(int, values)
        except (ValueError, TypeError) as exc:
            raise ValueError("The bounding-box filter returned incomplete pixel bounds") from exc
        if not (0 <= x1 <= x2 < frame.width and 0 <= y1 <= y2 < frame.height):
            raise ValueError("The bounding-box filter returned invalid pixel bounds")
        return x1, y1, x2, y2


def _scan_window(
    container: Any, stream: Any, source: dict[str, Any], window: dict[str, Any], limit: float,
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[dict[str, Any], Bounds | None, set[str]]:
    start_time = _stream_start(container, stream)
    first_time = start_time + Fraction(str(window["start"]))
    end_time = first_time + Fraction(str(window["duration"]))
    if stream.time_base is None:
        raise EncodingError("The video has no timestamp time base for crop detection")
    container.seek(int(first_time / stream.time_base), stream=stream, backward=True)
    reader = _LumaBounds(limit)
    bounds: Bounds | None = None
    uncertainty: set[str] = set()
    diagnostics = dict(window, frames=0, content_frames=0, black_frames=0, uncertain_frames=0)

    def observe(frame: Any) -> None:
        nonlocal bounds
        diagnostics["frames"] += 1
        try:
            content = reader.read(frame)
        except (ValueError, av.FFmpegError) as exc:
            diagnostics["uncertain_frames"] += 1
            uncertainty.add(str(exc))
            return
        if content is None:
            diagnostics["black_frames"] += 1
        else:
            diagnostics["content_frames"] += 1
            bounds = _union(bounds, content)

    previous = None
    previous_time: Fraction | None = None
    decoder = container.decode(stream)
    try:
        for frame in decoder:
            if check_cancelled:
                check_cancelled()
            if (frame.width, frame.height) != (source["width"], source["height"]):
                raise EncodingError("The input changes video dimensions during crop detection")
            if frame.pts is None or frame.time_base is None:
                raise EncodingError("Crop detection requires video presentation timestamps")
            timestamp = frame.pts * Fraction(frame.time_base)
            if previous_time is not None and timestamp <= previous_time:
                raise EncodingError("Video presentation timestamps are not increasing during crop detection")
            if timestamp < first_time:
                previous, previous_time = frame, timestamp
                continue
            # A low-frame-rate frame may begin before the detection window and
            # remain visible inside it. Inspect that overlapping frame too.
            if previous is not None and timestamp > first_time:
                observe(previous)
            previous = None
            previous_time = timestamp
            if timestamp >= end_time:
                break
            observe(frame)
        else:
            if previous is not None:
                frame_end = (
                    previous_time + previous.duration * Fraction(previous.time_base)
                    if previous.duration and previous.duration > 0
                    else start_time + Fraction(str(source["duration"]))
                )
                if frame_end > first_time:
                    observe(previous)
    finally:
        decoder.close()
    diagnostics.update({
        "bounds": _bounds_dict(bounds), "pixel_format": reader.format,
        "bit_depth": reader.depth, "threshold": reader.threshold,
    })
    return diagnostics, bounds, uncertainty


def detect_crop(
    path: str | Path,
    video_index: int,
    samples: list[dict[str, Any]],
    source: dict[str, Any],
    settings: dict[str, Any],
    check_cancelled: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Return one conservative crop for every CRF trial, or retain full frame.

    ``limit`` is a fraction of the raw luminance code range, not a percentage
    of visible brightness. Native 8/10-bit Y values remain in their original
    range; no resizing, RGB conversion, or tone mapping is involved.
    """
    limit = _number(settings.get("limit", _DEFAULT_LIMIT), "Crop detection limit")
    if limit > 1:
        raise EncodingError("Crop detection limit must be between 0 and 1")
    seconds = _number(settings.get("seconds", 2.0), "Crop detection seconds", positive=True)
    width, height = source["width"], source["height"]
    windows = _windows(samples, source["duration"], seconds)
    result: dict[str, Any] = {
        "mode": "auto", "detector": "pyav-bbox-luma", "crop": None,
        "width": width, "height": height, "limit": limit,
        "threshold_semantics": "floor(limit * (2**bit_depth - 1)); raw luminance values above threshold are content",
        "seconds_per_window": seconds, "window_strategy": "centered",
        "windows": [], "frames": 0, "content_frames": 0, "black_frames": 0,
        "uncertain_frames": 0, "bounds": None,
    }

    def finish(code: str, reason: str) -> dict[str, Any]:
        result.update(reason_code=code, reason=reason)
        return result

    if not windows:
        return finish("no_windows", "No valid detection windows; kept the full frame")
    if "bbox" not in av.filter.filters_available:
        return finish("detector_unavailable", "This PyAV build has no bbox filter; kept the full frame")
    bounds: Bounds | None = None
    uncertainty: set[str] = set()
    try:
        # bbox emits a diagnostic for every frame. Its structured metadata is
        # the source of measurements; suppress routine native console output.
        with av.logging.Capture(local=False):
            with av.open(str(Path(path).expanduser().resolve()), mode="r") as container:
                stream = _video_stream(container, video_index)
                for window in windows:
                    if check_cancelled:
                        check_cancelled()
                    details, content, problems = _scan_window(container, stream, source, window, limit, check_cancelled)
                    result["windows"].append(details)
                    for key in ("frames", "content_frames", "black_frames", "uncertain_frames"):
                        result[key] += details[key]
                    uncertainty.update(problems)
                    if not details["frames"]:
                        uncertainty.add("A detection window contained no video frames")
                    if content is not None:
                        bounds = _union(bounds, content)
    except av.FFmpegError as exc:
        raise EncodingError(f"Cannot decode video for crop detection: {exc}") from exc
    result["bounds"] = _bounds_dict(bounds)
    if uncertainty:
        result["uncertainty_reasons"] = sorted(uncertainty)
        return finish("uncertain_detection", "Some sampled frames could not be measured reliably; kept the full frame")
    if bounds is None:
        return finish("no_content", "All sampled frames were below the luminance threshold; kept the full frame")
    x1, y1, x2, y2 = bounds
    if bounds == (0, 0, width - 1, height - 1):
        return finish("full_frame_content", "Observed content reaches the full frame; no consistent black margins")
    active_width, active_height = x2 - x1 + 1, y2 - y1 + 1
    if (active_width * 2 < width or active_height * 2 < height
            or active_width * active_height * 2 < width * height):
        return finish("small_content", "Detected content is too small to distinguish margins from a dark scene; kept the full frame")
    # Offsets may be odd even when the picture dimensions are even. Expanding
    # both edges to even coordinates would turn an 804-line picture at y=137
    # into 806 lines. Preserve the measured rectangle; encoder preflight checks
    # dimension compatibility separately without changing these pixel bounds.
    result.update(crop=f"{active_width}:{active_height}:{x1}:{y1}",
                  width=active_width, height=active_height)
    return finish("detected_margins", "Exact crop preserves the union of content in all detection windows")
