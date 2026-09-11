"""Regression coverage for native QP metrics and presentation-time sampling."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import av

from scripts import crf_encode as backend


def make_vfr_video(path: Path) -> list[int]:
    """Create B-frame source video with a nonzero start and varying durations."""
    times = [500, 540, 600, 630, 710, 750, 810, 850, 930, 1000, 1040, 1100]
    durations = {time: later - time for time, later in zip(times, times[1:])}
    durations[times[-1]] = 60
    with av.open(str(path), "w") as output:
        stream = output.add_stream("libx264", rate=25)
        stream.width, stream.height = 96, 64
        stream.pix_fmt = "yuv420p"
        stream.time_base = Fraction(1, 1000)
        stream.codec_context.time_base = Fraction(1, 1000)
        stream.options = {
            "preset": "ultrafast", "crf": "18", "thread_type": "0", "threads": "1",
            "x264-params": "bframes=2:b-adapt=0:scenecut=0:keyint=250",
        }

        def mux(packets):
            for packet in packets:
                packet.duration = durations[packet.pts]
                output.mux(packet)

        for index, timestamp in enumerate(times):
            frame = av.VideoFrame(96, 64, "yuv420p")
            for plane_index, plane in enumerate(frame.planes):
                value = (32 + index * 5) if plane_index == 0 else 128
                plane.update(bytes([value]) * plane.buffer_size)
            frame.pts, frame.time_base = timestamp, Fraction(1, 1000)
            frame.duration = durations[timestamp]
            mux(stream.encode(frame))
        mux(stream.encode(None))
    return times


class QpParserTests(unittest.TestCase):
    def test_x264_missing_b_frames_is_valid(self):
        qp, counts = backend.parse_x264_summary([
            (32, "libx264", "frame I:1 Avg QP: 9.52 size:123\n"),
            (32, "libx264", "frame P:3 Avg QP: 15.60 size:456\n"),
        ])
        self.assertEqual(qp, {"I": 9.52, "P": 15.60})
        self.assertEqual(counts, {"I": 1, "P": 3})

    def test_x264_rejects_missing_or_malformed_summary(self):
        for logs in ([], [(32, "libx264", "frame I:1 Avg QP: nan")]):
            with self.subTest(logs=logs), self.assertRaises(backend.EncodingError):
                backend.parse_x264_summary(logs)

    def test_x265_csv_normalizes_types_and_averages_by_frame_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "stats.csv"
            path.write_text(
                "Layer , Encode Order, Type, POC, QP, Bits\n"
                "0, 0, I-SLICE, 0, 16.0, 10\n"
                "0, 1, P-SLICE, 3, 18.0, 20\n"
                "0, 2, B-SLICE, 1, 19.0, 30\n"
                "0, 3, b-SLICE, 2, 21.0, 40\n",
                encoding="utf-8",
            )
            qp, counts = backend.parse_x265_csv(path)
            self.assertEqual(qp, {"I": 16.0, "P": 18.0, "B": 20.0})
            self.assertEqual(counts, {"I": 1, "P": 1, "B": 2})
            path.write_text("Type, QP\nI-SLICE, nan\n", encoding="utf-8")
            with self.assertRaisesRegex(backend.EncodingError, "non-finite"):
                backend.parse_x265_csv(path)


class NativeSamplingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            av.Codec("libx264", "w")
        except (av.FFmpegError, ValueError) as exc:
            raise unittest.SkipTest("PyAV wheel has no libx264 encoder") from exc
        cls.temporary = tempfile.TemporaryDirectory(prefix="test-crf-backend-")
        cls.source = Path(cls.temporary.name) / "offset-vfr.mkv"
        make_vfr_video(cls.source)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def job(self):
        return {
            "input": str(self.source), "video_index": 0, "codec": "x264",
            "start": 0.035, "duration": 0.220, "crf": 18,
            "settings": {
                "preset": "ultrafast", "pixel_format": "yuv420p",
                "params": {"bframes": "2", "b-adapt": "0", "scenecut": "0"},
                "options": {"threads": "1"},
            },
        }

    def test_nonzero_start_duration_uses_video_timeline(self):
        source = backend.inspect_video(self.source)
        self.assertAlmostEqual(source["start_time"], 0.5)
        self.assertAlmostEqual(source["duration"], 0.66)
        self.assertEqual(source["duration_source"], "video_duration_tag")

    def test_vfr_seek_crop_and_delayed_packets_preserve_presentation_span(self):
        job = self.job()
        job["crop"] = "64:48:2:4"
        with contextlib.redirect_stderr(io.StringIO()):
            result = backend.encode_sample(job)
        # Frames start at .04, .10, .13, .21, .25. The next frame starts
        # at .31, so their original presentation span is .27 seconds.
        self.assertEqual(result["frames"], 5)
        self.assertEqual(sum(result["frame_counts"].values()), 5)
        self.assertGreater(result["frame_counts"].get("B", 0), 0)
        self.assertAlmostEqual(result["actual_start"], 0.04)
        self.assertAlmostEqual(result["actual_end"], 0.31)
        self.assertAlmostEqual(result["duration"], 0.27)
        self.assertEqual((result["width"], result["height"]), (64, 48))
        self.assertGreater(result["video_bytes"], 0)

    def test_last_frame_uses_its_duration_at_end_of_file(self):
        job = self.job()
        job.update(start=0.6, duration=1.0)
        with contextlib.redirect_stderr(io.StringIO()):
            result = backend.encode_sample(job)
        self.assertEqual(result["frames"], 1)
        self.assertAlmostEqual(result["duration"], 0.06)
        self.assertEqual(result["frame_counts"], {"I": 1})

    def test_unknown_native_and_ffmpeg_options_are_rejected(self):
        for field in ("params", "options"):
            job = self.job()
            job["settings"][field]["made-up-crf-option"] = "1"
            with self.subTest(field=field), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaisesRegex(backend.EncodingError, "[Uu]nrecognized|rejected"):
                    backend.encode_sample(job)

    def test_invalid_crop_and_incompatible_level_are_rejected(self):
        source = backend.inspect_video(self.source)
        settings = self.job()["settings"]
        for crop in ("0:48:0:0", "98:64:0:0", "65:48:0:0", "iw:ih:0:0"):
            with self.subTest(crop=crop), self.assertRaises(backend.EncodingError):
                backend.validate_source_settings(source, "x264", settings, crop)
        settings["level"] = "4.1"
        source.update(width=3840, height=2160)
        with self.assertRaisesRegex(backend.EncodingError, "level 4.1"):
            backend.validate_source_settings(source, "x264", settings)


class ExactCropEncodingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="test-crf-exact-crop-")
        cls.sources = {}
        for top in (137, 139):
            path = Path(cls.temporary.name) / f"letterbox-{top}.mkv"
            frame = av.VideoFrame(1920, 1080, "yuv420p")
            for index, plane in enumerate(frame.planes):
                plane.update(bytes([16 if index == 0 else 128]) * plane.buffer_size)
            plane = frame.planes[0]
            pixels = bytearray(bytes(plane))
            for relative_y in range(804):
                # Distinct neighboring rows expose an incorrectly rounded
                # vertical origin; marked columns also test an odd x origin.
                row = bytearray([32 + relative_y % 192]) * 1920
                row[0], row[1], row[-2], row[-1] = 40, 80, 100, 160
                offset = (top + relative_y) * plane.line_size
                pixels[offset:offset + 1920] = row
            plane.update(pixels)
            frame.pts, frame.time_base, frame.duration = 0, Fraction(1, 25), 1
            with av.open(str(path), "w") as output:
                stream = output.add_stream("ffv1", rate=25)
                stream.width, stream.height, stream.pix_fmt = 1920, 1080, "yuv420p"
                for packet in stream.encode(frame):
                    output.mux(packet)
                for packet in stream.encode(None):
                    output.mux(packet)
            cls.sources[top] = path

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def assert_exact_crop(self, codec, *, top, left=0, width=1920):
        library = "libx264" if codec == "x264" else "libx265"
        try:
            av.Codec(library, "w")
        except (av.FFmpegError, ValueError) as exc:
            raise unittest.SkipTest(f"PyAV wheel has no {library} encoder") from exc
        observed = []
        compressed = []

        class RecordingEncoder:
            """Observe the real pipeline input while still using the native codec."""

            def __init__(self, wrapped):
                object.__setattr__(self, "wrapped", wrapped)

            def __getattr__(self, name):
                return getattr(self.wrapped, name)

            def __setattr__(self, name, value):
                setattr(self.wrapped, name, value)

            def encode(self, frame=None):
                if frame is not None:
                    plane = frame.planes[0]
                    sample_bytes = 1 if frame.format.components[0].bits == 8 else 2
                    data = memoryview(plane)

                    def pixel(x, y):
                        offset = y * plane.line_size + x * sample_bytes
                        return int.from_bytes(data[offset:offset + sample_bytes], "little")

                    observed.append({
                        "size": (frame.width, frame.height),
                        "top": pixel(frame.width // 2, 0),
                        "bottom": pixel(frame.width // 2, frame.height - 1),
                        "left": pixel(0, 0), "right": pixel(frame.width - 1, 0),
                    })
                packets = self.wrapped.encode(frame)
                compressed.extend(bytes(packet) for packet in packets)
                return packets

        class RecordingAv:
            CodecContext = SimpleNamespace(
                create=lambda name, mode: RecordingEncoder(av.CodecContext.create(name, mode))
            )

            def __getattr__(self, name):
                return getattr(av, name)

        params = {"bframes": "0", "rc-lookahead": "0"}
        if codec == "x265":
            params.update(pools="none", **{"frame-threads": "1"})
        settings = {
            "preset": "ultrafast", "pixel_format": "yuv420p" if codec == "x264" else "yuv420p10le",
            "profile": "high" if codec == "x264" else "main10",
            "params": params, "options": {"threads": "1"},
        }
        job = {
            "input": str(self.sources[top]), "start": 0.0, "duration": 0.04,
            "codec": codec, "crf": 18, "crop": f"{width}:804:{left}:{top}",
            "settings": settings,
        }
        with patch.object(backend, "av", RecordingAv()), contextlib.redirect_stderr(io.StringIO()):
            result = backend.encode_sample(job)
        scale = 1 if codec == "x264" else 4
        self.assertEqual(result["frames"], 1)
        self.assertEqual((result["width"], result["height"]), (width, 804))
        self.assertEqual(observed, [{
            "size": (width, 804), "top": 32 * scale, "bottom": 67 * scale,
            "left": (80 if left else 40) * scale,
            "right": (100 if left else 160) * scale,
        }])
        self.assertEqual(result["frame_counts"], {"I": 1})
        # Decode the actual compressed packets too: a correct reported size
        # alone would not catch padding or changed conformance dimensions.
        decoder = av.CodecContext.create("h264" if codec == "x264" else "hevc", "r")
        decoded = []
        for payload in compressed:
            decoded.extend(decoder.decode(av.Packet(payload)))
        decoded.extend(decoder.decode(None))
        self.assertEqual([(frame.width, frame.height) for frame in decoded], [(width, 804)])

    def test_x264_preserves_804_rows_at_odd_vertical_offsets(self):
        for top in (137, 139):
            with self.subTest(top=top):
                self.assert_exact_crop("x264", top=top)

    def test_x265_preserves_804_rows_at_odd_vertical_offsets(self):
        for top in (137, 139):
            with self.subTest(top=top):
                self.assert_exact_crop("x265", top=top)

    def test_odd_horizontal_offset_preserves_the_requested_source_column(self):
        self.assert_exact_crop("x264", top=137, left=1, width=1918)


if __name__ == "__main__":
    unittest.main()
