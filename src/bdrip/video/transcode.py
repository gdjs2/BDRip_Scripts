"""Run one native x264/x265 bitrate pass through PyAV in a Python worker."""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
from fractions import Fraction
from pathlib import Path

import av

from bdrip.common.progress import FrameProgress

from .crop import ExactCrop
from .encode import validate_source_settings
from .probe import _COLOR_ATTRIBUTES, EncodingError, _rate, _video_stream, inspect_video


def _timed_frames(container, stream, source):
    """Keep presentation times, including VFR intervals and the last frame."""
    previous = previous_time = last_step = None
    decoder = container.decode(stream)
    try:
        for frame in decoder:
            if (frame.width, frame.height) != (source["width"], source["height"]):
                raise EncodingError("The input changes video dimensions while decoding")
            if frame.pts is None or frame.time_base is None:
                raise EncodingError(
                    "Decoded video frames have no presentation timestamps"
                )
            timestamp = frame.pts * Fraction(frame.time_base)
            if previous is not None:
                last_step = timestamp - previous_time
                if last_step <= 0:
                    raise EncodingError("Video presentation timestamps must increase")
                yield previous, previous_time, last_step
            previous, previous_time = frame, timestamp
        if previous is not None:
            duration = (
                previous.duration * Fraction(previous.time_base)
                if previous.duration and previous.duration > 0
                else last_step or 1 / _rate(stream)
            )
            yield previous, previous_time, duration
    finally:
        decoder.close()


def encode_pass(job: dict) -> dict:
    """Write first-pass statistics or a video-only Matroska second pass."""
    phase = job["pass"]
    if phase not in (1, 2):
        raise EncodingError("Pass must be 1 or 2")
    codec = job["codec"]
    if codec not in {"x264", "x265"}:
        raise EncodingError("Codec must be x264 or x265")
    stats = Path(
        "pass.stats"
    )  # Worker cwd is a private directory; paths need no escaping.
    if phase == 2 and (not stats.is_file() or not stats.stat().st_size):
        raise EncodingError("The second pass requires completed first-pass statistics")
    source = inspect_video(job["input"])
    settings = {"pixel_format": job["pixel_format"]}
    width, height = validate_source_settings(source, codec, settings, job.get("crop"))
    options = {"preset": job.get("preset", "veryslow"), "thread_type": "0"}
    if codec == "x264":
        options["stats"] = str(stats)
    else:
        # Explicit native options also work with bundled libavcodec versions
        # that do not map generic PASS1/PASS2 flags into libx265 parameters.
        options["x265-params"] = f"pass={phase}:stats={stats}"
    output = video = encoder = frame = packet = frames_iterator = None
    frames = packets = video_bytes = 0
    first = final = None
    try:
        with av.open(job["input"]) as container:
            stream = _video_stream(container, 0)
            rate = _rate(stream)
            time_base = Fraction(stream.time_base)
            with av.open(
                os.devnull if phase == 1 else job["output"],
                "w",
                format="null" if phase == 1 else "matroska",
            ) as output:
                video = output.add_stream(f"lib{codec}", rate=rate)
                encoder = video.codec_context
                encoder.width, encoder.height = width, height
                encoder.pix_fmt = job["pixel_format"]
                encoder.bit_rate = job["bitrate"] * 1000
                encoder.time_base = video.time_base = time_base
                encoder.framerate = rate
                encoder.sample_aspect_ratio = stream.sample_aspect_ratio or Fraction(1)
                for name in _COLOR_ATTRIBUTES:
                    setattr(encoder, name, getattr(stream.codec_context, name))
                encoder.flags |= (
                    av.codec.context.Flags.pass1
                    if phase == 1
                    else av.codec.context.Flags.pass2
                )
                encoder.options = options
                encoder.open()
                if encoder.options:
                    raise EncodingError(
                        f"Unrecognized encoder options: {encoder.options}"
                    )
                crop = ExactCrop(job.get("crop"))
                progress = FrameProgress(job.get("progress_file"))
                frames_iterator = _timed_frames(container, stream, source)
                for frame, timestamp, duration in frames_iterator:
                    if first is None:
                        first = timestamp
                    frame = crop.apply(frame).reformat(format=encoder.pix_fmt)
                    ticks = (timestamp - first) / time_base
                    if ticks.denominator != 1:
                        raise EncodingError(
                            "Frame timestamps cannot be represented exactly"
                        )
                    frame.pts, frame.time_base = int(ticks), time_base
                    frame.duration = max(1, round(duration / time_base))
                    frame.pict_type = av.video.frame.PictureType.NONE
                    for packet in video.encode(frame):
                        packets += 1
                        video_bytes += packet.size
                        output.mux(packet)
                    frames += 1
                    final = timestamp + duration
                    progress.update(frames, float(final - first) / source["duration"])
                if not frames:
                    raise EncodingError("The selected video stream contains no frames")
                progress.update(frames, 0.99, flushing=True)
                for packet in video.encode(None):
                    packets += 1
                    video_bytes += packet.size
                    output.mux(packet)
                if packets != frames or video_bytes <= 0:
                    raise EncodingError(
                        f"Encoder produced {packets} packets for {frames} frames"
                    )
        return {
            "frames": frames,
            "duration": float(final - first),
            "video_bytes": video_bytes,
            "width": width,
            "height": height,
            "pixel_format": job["pixel_format"],
            "pass": phase,
        }
    except av.FFmpegError as exc:
        raise EncodingError(f"{codec} pass {phase} failed: {exc}") from exc
    finally:
        if frames_iterator is not None:
            frames_iterator.close()
        # Packets retain their stream/context. Release them before checking the
        # stats file: x264 finalizes its .temp file in avcodec_free_context.
        frame = packet = encoder = video = output = None
        gc.collect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", type=Path, required=True)
    args = parser.parse_args()
    # In this isolated worker, both libavcodec and x265 diagnostics go straight
    # to stderr, which the parent keeps in the task's encoding log.
    av.logging.set_level(av.logging.INFO)
    av.logging.restore_default_callback()
    try:
        job = json.loads(args.job.read_text(encoding="utf-8"))
        print(json.dumps(encode_pass(job), allow_nan=False))
        return 0
    except (
        EncodingError,
        av.FFmpegError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
    ) as exc:
        print(f"Encoding failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
