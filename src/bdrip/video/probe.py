"""Inspect video streams and their presentation timing through PyAV."""

from __future__ import annotations

import re
from fractions import Fraction
from pathlib import Path
from typing import Any

import av


class EncodingError(RuntimeError):
    """An input or encoder cannot produce a trustworthy sample measurement."""


_COLOR_ATTRIBUTES = ("color_range", "color_primaries", "color_trc", "colorspace")


def _video_stream(container: Any, video_index: int) -> Any:
    streams = [
        stream
        for stream in container.streams.video
        if not stream.disposition & av.stream.Disposition.attached_pic
    ]
    if (
        isinstance(video_index, bool)
        or not isinstance(video_index, int)
        or video_index < 0
    ):
        raise EncodingError("video_index must be a nonnegative integer")
    if video_index >= len(streams):
        raise EncodingError(
            f"Video stream {video_index} is unavailable; found {len(streams)} "
            "video stream(s), excluding attached pictures"
        )
    return streams[video_index]


def _rate(stream: Any) -> Fraction:
    for name in ("average_rate", "guessed_rate", "base_rate"):
        rate = getattr(stream, name, None)
        if rate is not None and rate > 0:
            return Fraction(rate)
    raise EncodingError("The selected video stream has no usable frame rate")


def _stream_start(container: Any, stream: Any) -> Fraction:
    if stream.start_time is not None and stream.time_base:
        return stream.start_time * Fraction(stream.time_base)
    if container.start_time is not None:
        return Fraction(container.start_time, av.time_base)
    return Fraction(0)


def _stream_duration(container: Any, stream: Any) -> tuple[Fraction, str]:
    if stream.duration is not None and stream.time_base and stream.duration > 0:
        return stream.duration * Fraction(stream.time_base), "video_stream"
    # Matroska commonly stores per-stream duration as a tag, rather than in
    # AVStream.duration. Prefer it to a container duration that includes audio.
    for key, value in stream.metadata.items():
        if key.upper().split("-")[0] != "DURATION":
            continue
        match = re.fullmatch(r"(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", value.strip())
        if match:
            hours, minutes, seconds = match.groups()
            duration = int(hours) * 3600 + int(minutes) * 60 + Fraction(seconds)
            # Matroska's muxer writes the stream's ending timestamp in this
            # tag; a nonzero starting timestamp is not additional video.
            if "matroska" in container.format.name or "webm" in container.format.name:
                duration -= _stream_start(container, stream)
            if duration > 0:
                return duration, "video_duration_tag"
    if container.duration is not None and container.duration > 0:
        return Fraction(container.duration, av.time_base), "container_fallback"
    raise EncodingError(
        "The selected video has no known duration. Use a seekable movie file "
        "with duration metadata."
    )


def inspect_video(path: str | Path, video_index: int = 0) -> dict[str, Any]:
    """Inspect the selected real video stream without decoding the movie."""
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise EncodingError(f"Input video does not exist: {path}")
    try:
        with av.open(str(path), mode="r") as container:
            stream = _video_stream(container, video_index)
            context = stream.codec_context
            duration, duration_source = _stream_duration(container, stream)
            rate = _rate(stream)
            result = {
                "duration": float(duration),
                "duration_source": duration_source,
                "start_time": float(_stream_start(container, stream)),
                "width": context.width,
                "height": context.height,
                "fps": str(rate),
                "pixel_format": context.format.name if context.format else None,
                "codec": context.name,
                "video_index": video_index,
                "stream_index": stream.index,
                "time_base": str(stream.time_base),
                "sample_aspect_ratio": str(stream.sample_aspect_ratio)
                if stream.sample_aspect_ratio
                else None,
            }
            result.update({name: getattr(context, name) for name in _COLOR_ATTRIBUTES})
            return result
    except av.FFmpegError as exc:
        raise EncodingError(f"Cannot inspect input video: {exc}") from exc
